#!/usr/bin/env python3
from __future__ import annotations
# ocp-quay-sync-tasks.py v1.02
#
# Inspect the repository-mirror queue on a regional Quay (OpenShift /
# Quay operator).  Read-only unless a write flag is given.
#
# Two Quay levels, two tool prefixes:
#   - Central quays (cquay0, cquay1, cquay3, ...) are RHEL hosts running
#     podman.  They sit in the DMZ, pull from the Red Hat CDN, and are
#     the source of truth.  Use the rhel-quay-* tools over SSH.
#   - Regional quays run on hub clusters, managed by the Quay operator.
#     They have no DMZ path.  Each one mirrors FROM its central quay.
#     One central quay feeds several regionals; each hub has zero or
#     more PoP spokes in ACM.  This script is the regional side only.
#
# On Quay 3.15 and 3.16 the mirror worker claims a row by setting
# SYNCING and a 12h expiration.  SYNC_NOW is the queue, not a stuck
# task: disabled mirrors (the usual regional pattern, is_enabled=false
# plus an on-demand sync) are claimed only while status is SYNC_NOW.
# Resetting those rows to NEVER_RUN drops them out of the queue.
# A SYNCING row whose expiration has passed is the one that is stuck,
# because a disabled mirror is not reclaimed by the worker.
#
# Status values are read from the running mirror pod.  Quay 3.15/3.16
# uses CANCEL=-2, FAIL=-1, NEVER_RUN=0, SUCCESS=1, SYNCING=2,
# SYNC_NOW=3.  An older map that called 2 FAIL and 4 SYNCING mislabels
# a live sync as a failure.
#
# Usage:
#   export KUBECONFIG=/path/to/hub-kubeconfig
#
#   # Read-only report (default):
#   ./ocp-quay-sync-tasks.py
#
#   # Follow the queue until workers have drained it:
#   ./ocp-quay-sync-tasks.py --watch 30
#
#   # Put abandoned SYNCING claims back on the queue:
#   ./ocp-quay-sync-tasks.py --requeue-stuck
#
# Supports Quay 3.15 and 3.16 (status map read from the pod).
# Namespace is discovered from the QuayRegistry, not hard-coded.
#
# Requirements:
#   - oc (OpenShift CLI) in PATH
#   - python3 (3.7+)
#
# License: Apache-2.0
#
# Maintainers & Contributors:
#   Vincent Cojot & Johann Peyrard
#

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Sync status values (from Quay data model)
# ---------------------------------------------------------------------------
# Operator Quay 3.15 / 3.16 (RepoMirrorStatus IntEnum).
MODERN_STATUS = {
    -2: "CANCEL",
    -1: "FAIL",
    0:  "NEVER_RUN",
    1:  "SUCCESS",
    2:  "SYNCING",
    3:  "SYNC_NOW",
}

# Pre-IntEnum layout.  Kept only if a pod still reports SYNCING=4.
LEGACY_STATUS = {
    0: "NEVER_RUN",
    1: "SUCCESS",
    2: "FAIL",
    3: "SYNC_NOW",
    4: "SYNCING",
}


class MirrorCodes:
    """Integer codes for one Quay's repomirrorconfig.sync_status."""

    def __init__(self, labels: dict[int, str], source: str):
        self.labels = dict(labels)
        self.source = source
        rev = {name: code for code, name in labels.items()}
        self.never_run = rev["NEVER_RUN"]
        self.success = rev["SUCCESS"]
        self.fail = rev["FAIL"]
        self.syncing = rev["SYNCING"]
        self.sync_now = rev["SYNC_NOW"]
        self.cancel = rev.get("CANCEL")
        self.known = set(labels)

    def label(self, code: int | None) -> str:
        if code is None:
            return "UNSET"
        return self.labels.get(code, f"UNKNOWN({code})")


def modern_codes() -> MirrorCodes:
    return MirrorCodes(MODERN_STATUS, "Quay 3.15/3.16 default")


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_IS_TTY = sys.stdout.isatty()

C_HDR = "\033[1;36m" if _IS_TTY else ""
C_OK  = "\033[1;32m" if _IS_TTY else ""
C_WRN = "\033[1;33m" if _IS_TTY else ""
C_ERR = "\033[1;31m" if _IS_TTY else ""
C_RST = "\033[0m"    if _IS_TTY else ""
C_DIM = "\033[2m"    if _IS_TTY else ""
C_BLD = "\033[1m"    if _IS_TTY else ""


def hdr(title: str) -> str:
    return f"\n{C_HDR}=== {title} ==={C_RST}"


def info(msg: str) -> str:
    return f"(II) {msg}"


def warn(msg: str) -> str:
    return f"{C_WRN}(WW) {msg}{C_RST}"


def err(msg: str) -> str:
    return f"{C_ERR}(EE) {msg}{C_RST}"


def ok(msg: str) -> str:
    return f"{C_OK}(OK) {msg}{C_RST}"


def parse_db_dt(dt_str: str) -> datetime | None:
    """Parse a Quay timestamp. Naive values are UTC (datetime.utcnow)."""
    if not dt_str or not dt_str.strip():
        return None
    text = dt_str.strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_duration(total_secs: int) -> str:
    total_secs = abs(int(total_secs))
    days = total_secs // 86400
    if days > 365:
        return f"{days // 365}y {(days % 365) // 30}mo"
    if days > 30:
        return f"{days // 30}mo {days % 30}d"
    if days > 0:
        hours = (total_secs % 86400) // 3600
        return f"{days}d {hours}h"
    hours = total_secs // 3600
    mins = (total_secs % 3600) // 60
    if hours > 0:
        return f"{hours}h {mins}m"
    if mins > 0:
        return f"{mins}m {total_secs % 60}s"
    return f"{total_secs}s"


def age_str(dt_str: str) -> str:
    """Return a human-readable age string from an ISO timestamp."""
    dt = parse_db_dt(dt_str)
    if dt is None:
        return "unknown"
    total_secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if total_secs < 0:
        return "in " + format_duration(total_secs)
    return format_duration(total_secs)


def remaining_str(dt_str: str) -> str:
    """How long until a claim expiration, or how long ago it expired."""
    if not (dt_str or "").strip():
        return "no claim"
    dt = parse_db_dt(dt_str)
    if dt is None:
        return "unparsed"
    total_secs = int((dt - datetime.now(timezone.utc)).total_seconds())
    if total_secs >= 0:
        return format_duration(total_secs) + " left"
    return "expired " + format_duration(total_secs)


# Age above which a FAIL row is marked as an old failure in the table.
STUCK_THRESHOLD_SECS = 86400     # 1 day


def age_seconds(dt_str: str) -> int | None:
    """Return age in seconds, or None if unparseable. Negative means future."""
    dt = parse_db_dt(dt_str)
    if dt is None:
        return None
    return int((datetime.now(timezone.utc) - dt).total_seconds())


def parse_int(value: str) -> int | None:
    text = (value or "").strip()
    if text == "":
        return None
    try:
        return int(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], timeout: int = 60) -> str:
    """Run a command and return stdout. Returns '' on any failure."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


# ---------------------------------------------------------------------------
# Progress indicator
# ---------------------------------------------------------------------------

def progress(msg: str):
    if sys.stderr.isatty():
        sys.stderr.write(f"\r{C_DIM}{msg}{C_RST}\033[K")
        sys.stderr.flush()


def progress_clear():
    if sys.stderr.isatty():
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()


# ===================================================================
# Pre-flight checks
# ===================================================================

def preflight():
    hub_api = run(["oc", "whoami", "--show-server"]).strip()
    if not hub_api:
        print(err("Cannot reach cluster. Check $KUBECONFIG or ~/.kube/config."))
        sys.exit(1)

    hub_fqdn = re.sub(r"https?://api\.", "", hub_api)
    hub_fqdn = re.sub(r":\d+/?$", "", hub_fqdn)
    hub_short = hub_fqdn.split(".")[0]

    return hub_api, hub_fqdn, hub_short


# ===================================================================
# Quay discovery
# ===================================================================

def discover_quay_registries() -> list[dict]:
    """Discover all QuayRegistry instances across all namespaces.

    Returns a list of dicts with keys:
        ns, cr, ver, db_pod, quay_db
    """
    progress("Discovering Quay registries...")

    # Find all QuayRegistry CRs cluster-wide
    raw = run([
        "oc", "get", "quayregistry", "-A",
        "-o", "jsonpath="
        "{range .items[*]}"
        "{.metadata.namespace}|{.metadata.name}|"
        "{.status.currentVersion}{'\\n'}"
        "{end}",
    ])

    if not raw or not raw.strip():
        progress_clear()
        print(info("No QuayRegistry resources found on this cluster."))
        print(info("This cluster does not appear to host a Quay registry."))
        sys.exit(0)

    registries = []
    for line in raw.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        quay_ns = parts[0].strip()
        quay_cr = parts[1].strip()
        quay_ver = parts[2].strip() if len(parts) > 2 else "unknown"
        quay_ver = quay_ver or "unknown"

        # DB pod discovery — try both labels (varies across Quay versions)
        db_pod = ""
        for label in ("quay-component=postgres",
                      "quay-component=quay-database"):
            candidate = run([
                "oc", "get", "pods", "-n", quay_ns, "-l", label,
                "-o", "jsonpath={.items[0].metadata.name}",
            ]).strip()
            if candidate:
                db_pod = candidate
                break

        if not db_pod:
            # Try by pod name pattern
            all_pods = run([
                "oc", "get", "pods", "-n", quay_ns, "-o", "name",
            ]).strip()
            for pod_line in all_pods.splitlines():
                pod_name = pod_line.replace("pod/", "").strip()
                if re.search(r"database|postgres", pod_name, re.I) \
                        and "clair" not in pod_name.lower():
                    db_pod = pod_name
                    break

        if not db_pod:
            print(warn(
                f"No DB pod found for QuayRegistry '{quay_cr}' "
                f"in namespace '{quay_ns}'. Skipping."))
            continue

        # Auto-detect DB user
        db_user = run([
            "oc", "exec", db_pod, "-n", quay_ns, "--",
            "psql", "-U", "postgres", "-t",
            "-c", "SELECT usename FROM pg_user WHERE usename != 'postgres';",
        ]).strip().split("\n")[0].strip()
        if not db_user:
            db_user = "postgres"

        # Auto-detect DB name
        db_names = run([
            "oc", "exec", db_pod, "-n", quay_ns, "--",
            "psql", "-U", db_user, "-t",
            "-c", "SELECT datname FROM pg_database "
                  "WHERE datname NOT IN ('postgres','template0','template1');",
        ]).strip()
        quay_db = ""
        for line in db_names.splitlines():
            if "quay" in line.lower():
                quay_db = line.strip()
                break
        if not quay_db:
            quay_db = f"{quay_cr}-quay-database"

        # Validate DB connectivity
        check = run([
            "oc", "exec", db_pod, "-n", quay_ns, "--",
            "psql", "-U", db_user, "-d", quay_db, "-c", "SELECT 1;",
        ])
        if not check:
            print(warn(
                f"Cannot connect to database '{quay_db}' via pod "
                f"'{db_pod}'. Skipping."))
            continue

        registries.append({
            "ns":      quay_ns,
            "cr":      quay_cr,
            "ver":     quay_ver,
            "db_pod":  db_pod,
            "db_user": db_user,
            "quay_db": quay_db,
        })

    progress_clear()

    if not registries:
        print(err("Found QuayRegistry resources but could not connect "
                   "to any database."))
        sys.exit(1)

    return registries


# ===================================================================
# Database helpers
# ===================================================================

def quay_psql(quay_ns: str, db_pod: str, quay_db: str, sql: str,
              db_user: str = "postgres") -> str:
    return run([
        "oc", "exec", db_pod, "-n", quay_ns, "--",
        "psql", "-U", db_user, "-d", quay_db,
        "-P", "pager=off", "-t", "-A", "-F|", "-c", sql,
    ])


def quay_psql_write(quay_ns: str, db_pod: str, quay_db: str, sql: str,
                    db_user: str = "postgres") -> int:
    """Run an UPDATE and return the affected row count."""
    out = run([
        "oc", "exec", db_pod, "-n", quay_ns, "--",
        "psql", "-U", db_user, "-d", quay_db,
        "-P", "pager=off", "-c", sql,
    ])
    match = re.search(r"UPDATE\s+(\d+)", out)
    return int(match.group(1)) if match else 0


# ===================================================================
# Status map, workers, queue
# ===================================================================

_STATUS_PROBE = (
    "from data.database import RepoMirrorStatus as S\n"
    "names = ('CANCEL','FAIL','NEVER_RUN','SUCCESS','SYNCING','SYNC_NOW')\n"
    "print(' '.join('%s=%d' % (n, int(getattr(S, n))) "
    "for n in names if hasattr(S, n)))\n"
)


def detect_status_map(quay_ns: str) -> MirrorCodes:
    """Read RepoMirrorStatus from a mirror pod. Fall back to 3.15/3.16."""
    pod = run([
        "oc", "get", "pods", "-n", quay_ns, "-l", "quay-component=quay-mirror",
        "-o", "jsonpath={.items[0].metadata.name}",
    ]).strip()
    if not pod:
        return modern_codes()
    raw = run([
        "oc", "exec", pod, "-n", quay_ns, "--",
        "python3", "-c", _STATUS_PROBE,
    ]).strip()
    labels: dict[int, str] = {}
    for token in raw.split():
        if "=" not in token:
            continue
        name, value = token.split("=", 1)
        code = parse_int(value)
        if code is None or name not in (
                "CANCEL", "FAIL", "NEVER_RUN", "SUCCESS", "SYNCING", "SYNC_NOW"):
            continue
        labels[code] = name
    needed = {"FAIL", "NEVER_RUN", "SUCCESS", "SYNCING", "SYNC_NOW"}
    if not needed.issubset(set(labels.values())):
        return modern_codes()
    return MirrorCodes(labels, "mirror pod")


def mirror_worker_counts(quay_ns: str) -> tuple[int, int]:
    """Return (running, total) mirror-worker pods."""
    raw = run([
        "oc", "get", "pods", "-n", quay_ns, "-l", "quay-component=quay-mirror",
        "-o", "jsonpath={range .items[*]}{.status.phase}{'\\n'}{end}",
    ])
    phases = [line.strip() for line in raw.splitlines() if line.strip()]
    running = sum(1 for phase in phases if phase == "Running")
    return running, len(phases)


def ssrf_allowed_hosts(quay_ns: str) -> list[str]:
    """Hostnames the regional mirror worker is allowed to pull from.

    Reads only the SSRF_ALLOWED_HOSTS list. The rest of the Quay config
    stays in the pod.
    """
    pod = run([
        "oc", "get", "pods", "-n", quay_ns, "-l", "quay-component=quay-mirror",
        "-o", "jsonpath={.items[0].metadata.name}",
    ]).strip()
    if not pod:
        return []
    probe = r"""
import pathlib
lines = pathlib.Path("/conf/stack/config.yaml").read_text().splitlines()
grab = False
hosts = []
for line in lines:
    if line.startswith("SSRF_ALLOWED_HOSTS:"):
        grab = True
        rest = line.split(":", 1)[1].strip()
        if rest and rest not in ("|", ">", "[]"):
            hosts.append(rest.strip("\"'"))
        continue
    if not grab:
        continue
    item = line.strip()
    if item.startswith("- "):
        hosts.append(item[2:].strip().strip("\"'"))
        continue
    if line.startswith((" ", "\t")):
        continue
    break
print("\n".join(hosts))
"""
    raw = run(["oc", "exec", pod, "-n", quay_ns, "--", "python3", "-c", probe])
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _task_from_row(parts: list[str], codes: MirrorCodes) -> dict | None:
    if len(parts) < 9:
        return None
    status_int = parse_int(parts[2])
    return {
        "org":        parts[0],
        "repo":       parts[1],
        "status":     codes.label(status_int),
        "status_int": status_int if status_int is not None else -999,
        "start_date": parts[3],
        "expiration": parts[4],
        "ext_ref":    parts[5],
        "retries":    parts[6],
        "enabled":    parts[7] in ("t", "true", "1"),
        "id":         parts[8],
    }


def _select_tasks(quay_ns, db_pod, quay_db, db_user, codes, where: str,
                  progress_msg: str) -> list[dict]:
    progress(progress_msg)
    sql = f"""
        SELECT u.username,
               r.name,
               rmc.sync_status,
               COALESCE(rmc.sync_start_date::text, ''),
               COALESCE(rmc.sync_expiration_date::text, ''),
               rmc.external_reference,
               rmc.sync_retries_remaining,
               rmc.is_enabled,
               rmc.id
        FROM repomirrorconfig rmc
        JOIN repository r ON rmc.repository_id = r.id
        JOIN "user" u ON r.namespace_user_id = u.id
        WHERE {where}
        ORDER BY rmc.sync_start_date ASC NULLS FIRST;
    """
    raw = quay_psql(quay_ns, db_pod, quay_db, sql, db_user)
    progress_clear()
    tasks = []
    if not raw or not raw.strip():
        return tasks
    for line in raw.strip().splitlines():
        parts = [part.replace("\r", "").strip() for part in line.split("|")]
        task = _task_from_row(parts, codes)
        if task:
            tasks.append(task)
    return tasks


def classify_inflight(tasks: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Split SYNCING/SYNC_NOW rows into stuck claims, live syncs, and queue.

    A disabled mirror is claimed only from SYNC_NOW.  SYNCING with an
    expiration still in the future is a worker doing the copy.  SYNCING
    whose expiration has passed (or was never set) will not be reclaimed
    while is_enabled is false, so that row is stuck.
    """
    stuck, running, queued = [], [], []
    for task in tasks:
        if task["status"] == "SYNC_NOW":
            queued.append(task)
            continue
        if task["status"] != "SYNCING":
            continue
        # age_seconds is positive once the expiration timestamp is past.
        # A non-empty value we cannot parse is left with the running
        # set so a live claim is not reported as abandoned.
        expiration = (task.get("expiration") or "").strip()
        claim_age = age_seconds(expiration)
        if not expiration or (claim_age is not None and claim_age >= 0):
            stuck.append(task)
        else:
            running.append(task)
    return stuck, running, queued


def get_sync_summary(quay_ns: str, db_pod: str, quay_db: str,
                     db_user: str, codes: MirrorCodes) -> dict:
    """Get a count of all mirror configs by status."""
    sql = """
        SELECT COALESCE(sync_status::text, ''), count(*)
        FROM repomirrorconfig
        GROUP BY sync_status
        ORDER BY sync_status NULLS FIRST;
    """
    raw = quay_psql(quay_ns, db_pod, quay_db, sql, db_user)
    summary: dict[str, int] = {}
    if not raw:
        return summary
    for line in raw.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        count = parse_int(parts[1]) or 0
        code = parse_int(parts[0])
        label = "UNSET" if parts[0].strip() == "" else codes.label(code)
        summary[label] = summary.get(label, 0) + count
    return summary


def get_inflight(quay_ns, db_pod, quay_db, db_user, codes: MirrorCodes):
    where = (f"rmc.sync_status IN ({codes.sync_now}, {codes.syncing})")
    return _select_tasks(
        quay_ns, db_pod, quay_db, db_user, codes, where,
        "Querying mirror queue...",
    )


def get_failed_syncs(quay_ns, db_pod, quay_db, db_user, codes: MirrorCodes):
    return _select_tasks(
        quay_ns, db_pod, quay_db, db_user, codes,
        f"rmc.sync_status = {codes.fail}",
        "Querying failed sync tasks...",
    )


def get_unset_count(quay_ns: str, db_pod: str, quay_db: str,
                    db_user: str = "postgres") -> int:
    """Count mirror configs with NULL sync_status."""
    raw = quay_psql(
        quay_ns, db_pod, quay_db,
        "SELECT count(*) FROM repomirrorconfig WHERE sync_status IS NULL;",
        db_user,
    )
    return parse_int(raw) or 0


def get_org_breakdown(quay_ns, db_pod, quay_db, db_user, codes: MirrorCodes):
    """Get per-org mirror sync status breakdown."""
    progress("Querying per-org mirror breakdown...")
    sql = f"""
        SELECT u.username,
               count(*) AS total,
               count(*) FILTER (WHERE rmc.sync_status = {codes.never_run}) AS never_run,
               count(*) FILTER (WHERE rmc.sync_status = {codes.success}) AS success,
               count(*) FILTER (WHERE rmc.sync_status = {codes.fail}) AS fail,
               count(*) FILTER (WHERE rmc.sync_status = {codes.sync_now}) AS sync_now,
               count(*) FILTER (WHERE rmc.sync_status = {codes.syncing}) AS syncing,
               count(*) FILTER (WHERE rmc.is_enabled = true) AS enabled,
               count(*) FILTER (WHERE rmc.is_enabled = false) AS disabled
        FROM repomirrorconfig rmc
        JOIN repository r ON rmc.repository_id = r.id
        JOIN "user" u ON r.namespace_user_id = u.id
        GROUP BY u.username
        ORDER BY count(*) DESC;
    """
    raw = quay_psql(quay_ns, db_pod, quay_db, sql, db_user)
    progress_clear()
    orgs = []
    if not raw or not raw.strip():
        return orgs
    for line in raw.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 9:
            continue
        orgs.append({
            "org":       parts[0],
            "total":     parse_int(parts[1]) or 0,
            "never_run": parse_int(parts[2]) or 0,
            "success":   parse_int(parts[3]) or 0,
            "fail":      parse_int(parts[4]) or 0,
            "sync_now":  parse_int(parts[5]) or 0,
            "syncing":   parse_int(parts[6]) or 0,
            "enabled":   parse_int(parts[7]) or 0,
            "disabled":  parse_int(parts[8]) or 0,
        })
    return orgs


def upstream_hosts(quay_ns, db_pod, quay_db, db_user) -> list[dict]:
    """Count mirror configs by upstream registry host and status."""
    sql = """
        SELECT split_part(external_reference, '/', 1),
               COALESCE(sync_status::text, ''),
               count(*)
        FROM repomirrorconfig
        GROUP BY 1, 2
        ORDER BY 3 DESC;
    """
    raw = quay_psql(quay_ns, db_pod, quay_db, sql, db_user)
    rows = []
    if not raw:
        return rows
    for line in raw.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        rows.append({
            "host": parts[0],
            "status": parts[1],
            "count": parse_int(parts[2]) or 0,
        })
    return rows


def recent_mirror_activity(quay_ns, db_pod, quay_db, db_user,
                           seconds: int) -> dict:
    """Count mirror audit events over the last `seconds`.

    Returns keys started, success, failed, tag_success, enqueued,
    enqueuers (list of {user, count}). Missing log tables yield zeros.
    """
    empty = {
        "started": 0, "success": 0, "failed": 0, "tag_success": 0,
        "enqueued": 0, "enqueuers": [], "failures": [],
    }
    seconds = max(1, int(seconds))
    table = ""
    for candidate in ("logentry3", "logentry2", "logentry"):
        probe = quay_psql(
            quay_ns, db_pod, quay_db,
            f"SELECT to_regclass('public.{candidate}');",
            db_user,
        ).strip()
        if probe and probe.lower() != "null":
            table = candidate
            break
    if not table:
        return empty

    counts_sql = f"""
        SELECT k.name, count(*)
        FROM {table} l
        JOIN logentrykind k ON k.id = l.kind_id
        WHERE l.datetime > now() - interval '{seconds} seconds'
          AND k.name IN (
            'repo_mirror_sync_started',
            'repo_mirror_sync_success',
            'repo_mirror_sync_failed',
            'repo_mirror_sync_tag_success'
          )
        GROUP BY k.name;
    """
    raw = quay_psql(quay_ns, db_pod, quay_db, counts_sql, db_user)
    by_name = {}
    if raw:
        for line in raw.strip().splitlines():
            parts = line.split("|")
            if len(parts) >= 2:
                by_name[parts[0]] = parse_int(parts[1]) or 0

    enqueue_sql = f"""
        SELECT COALESCE(u.username, ''), count(*)
        FROM {table} l
        JOIN logentrykind k ON k.id = l.kind_id
        LEFT JOIN "user" u ON u.id = l.performer_id
        WHERE l.datetime > now() - interval '{seconds} seconds'
          AND k.name = 'repo_mirror_config_changed'
          AND l.metadata_json::json->>'changed' = 'sync_status'
          AND l.metadata_json::json->>'to' = 'SYNC_NOW'
        GROUP BY 1
        ORDER BY 2 DESC
        LIMIT 5;
    """
    raw = quay_psql(quay_ns, db_pod, quay_db, enqueue_sql, db_user)
    enqueuers = []
    enqueued = 0
    if raw:
        for line in raw.strip().splitlines():
            parts = line.split("|")
            if len(parts) < 2:
                continue
            count = parse_int(parts[1]) or 0
            enqueued += count
            enqueuers.append({"user": parts[0] or "unknown", "count": count})

    fail_sql = f"""
        SELECT left(COALESCE(l.metadata_json::json->>'message', ''), 110),
               count(*)
        FROM {table} l
        JOIN logentrykind k ON k.id = l.kind_id
        WHERE l.datetime > now() - interval '{seconds} seconds'
          AND k.name = 'repo_mirror_sync_failed'
        GROUP BY 1
        ORDER BY 2 DESC
        LIMIT 5;
    """
    raw = quay_psql(quay_ns, db_pod, quay_db, fail_sql, db_user)
    failures = []
    if raw:
        for line in raw.strip().splitlines():
            parts = line.split("|")
            if len(parts) >= 2 and parts[0]:
                failures.append({
                    "message": parts[0],
                    "count": parse_int(parts[1]) or 0,
                })

    return {
        "started": by_name.get("repo_mirror_sync_started", 0),
        "success": by_name.get("repo_mirror_sync_success", 0),
        "failed": by_name.get("repo_mirror_sync_failed", 0),
        "tag_success": by_name.get("repo_mirror_sync_tag_success", 0),
        "enqueued": enqueued,
        "enqueuers": enqueuers,
        "failures": failures,
    }


def requeue_where(quay_ns, db_pod, quay_db, db_user, codes: MirrorCodes,
                  where_sql: str) -> int:
    """Put rows back where the mirror worker will claim them.

    Enabled mirrors return to NEVER_RUN and are picked up on their
    schedule.  Disabled mirrors (regional on-demand sync) are set to
    SYNC_NOW, which is the only state the worker claims when
    is_enabled is false.  Live expiration timestamps are cleared.
    """
    sql = f"""
        UPDATE repomirrorconfig
        SET sync_status = CASE
                WHEN is_enabled THEN {codes.never_run}
                ELSE {codes.sync_now}
            END,
            sync_retries_remaining = 3,
            sync_expiration_date = NULL
        WHERE {where_sql};
    """
    return quay_psql_write(quay_ns, db_pod, quay_db, sql, db_user)


def stuck_claim_where(codes: MirrorCodes) -> str:
    return (
        f"sync_status = {codes.syncing} "
        f"AND (sync_expiration_date IS NULL "
        f"OR sync_expiration_date < now())"
    )


# ===================================================================
# Rendering
# ===================================================================

def render_summary_table(summary: dict) -> list[str]:
    """Render the status summary table."""
    lines = []
    total = sum(summary.values())
    lines.append(f"  Total mirror configurations: {total}")
    lines.append("")
    fmt = "  {:<14s}  {:>6s}"
    lines.append(fmt.format("STATUS", "COUNT"))
    lines.append("  " + "-" * 24)
    order = ("NEVER_RUN", "SUCCESS", "FAIL", "CANCEL",
             "SYNC_NOW", "SYNCING", "UNSET")
    for status_name in order:
        count = summary.get(status_name, 0)
        if count == 0 and status_name in ("UNSET", "CANCEL"):
            continue
        marker = ""
        if status_name == "SYNC_NOW" and count > 0:
            marker = f"  {C_OK}<-- queued for a worker{C_RST}"
        elif status_name == "SYNCING" and count > 0:
            marker = f"  {C_OK}<-- worker is copying{C_RST}"
        elif status_name == "FAIL" and count > 0:
            marker = f"  {C_WRN}<-- check below{C_RST}"
        elif status_name == "UNSET" and count > 0:
            marker = f"  {C_DIM}(NULL in DB){C_RST}"
        lines.append(fmt.format(status_name, str(count)) + marker)
    for status_name, count in summary.items():
        if status_name not in order:
            lines.append(fmt.format(status_name, str(count)))
    return lines


def render_task_table(
    tasks: list[dict], title_prefix: str, status_label: str,
) -> list[str]:
    """Render a table of sync tasks grouped by org."""
    lines = []
    if not tasks:
        return lines

    # Group by org
    orgs: dict[str, list[dict]] = {}
    for s in tasks:
        orgs.setdefault(s["org"], []).append(s)

    w_repo = max((len(s["repo"]) for s in tasks), default=10)
    w_repo = max(w_repo, 10)
    w_ref = max((len(s["ext_ref"]) for s in tasks), default=16)
    w_ref = min(max(w_ref, 16), 70)

    fmt = "  {:<{w_repo}s}  {:<9s}  {:>8s}  {:>7s}  {:<{w_ref}s}"
    header = fmt.format(
        "REPOSITORY", "STATUS", status_label, "RETRIES", "EXTERNAL SOURCE",
        w_repo=w_repo, w_ref=w_ref,
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))

    for org_name in sorted(orgs.keys()):
        org_tasks = orgs[org_name]
        lines.append(f"  {C_BLD}{org_name}/{C_RST} ({len(org_tasks)} repos)")
        for s in org_tasks:
            ext = s["ext_ref"]
            if len(ext) > w_ref:
                ext = ext[:w_ref - 3] + "..."
            marker = ""
            if s.get("status") == "SYNCING":
                age = remaining_str(s.get("expiration", ""))
                claim_age = age_seconds(s.get("expiration", ""))
                if claim_age is None:
                    marker = f"  {C_ERR}<-- no claim{C_RST}"
                elif claim_age >= 0:
                    marker = f"  {C_ERR}<-- claim expired{C_RST}"
            else:
                age = age_str(s["start_date"])
                secs = age_seconds(s["start_date"])
                if (s.get("status") == "FAIL"
                        and secs is not None and secs > STUCK_THRESHOLD_SECS):
                    marker = f"  {C_WRN}<-- old failure{C_RST}"
            lines.append(fmt.format(
                s["repo"],
                s["status"],
                age,
                s["retries"],
                ext,
                w_repo=w_repo,
                w_ref=w_ref,
            ) + marker)
        lines.append("")

    return lines


def render_age_breakdown(tasks: list[dict]) -> list[str]:
    """Render an age breakdown of sync tasks."""
    lines = []
    if not tasks:
        return lines

    lines.append("  Age breakdown:")
    buckets = {"< 1 day": 0, "1-7 days": 0, "1-4 weeks": 0,
               "1-3 months": 0, "> 3 months": 0, "unknown": 0}
    now = datetime.now(timezone.utc)
    for s in tasks:
        dt = parse_db_dt(s["start_date"])
        if dt is None:
            buckets["unknown"] += 1
            continue
        days = (now - dt).days
        if days < 1:
            buckets["< 1 day"] += 1
        elif days < 7:
            buckets["1-7 days"] += 1
        elif days < 28:
            buckets["1-4 weeks"] += 1
        elif days < 90:
            buckets["1-3 months"] += 1
        else:
            buckets["> 3 months"] += 1

    for label, count in buckets.items():
        if count > 0:
            marker = ""
            if label in ("> 3 months", "1-3 months"):
                marker = f"  {C_ERR}<-- likely stuck{C_RST}"
            elif label in ("1-4 weeks",):
                marker = f"  {C_WRN}<-- suspicious{C_RST}"
            lines.append(f"    {label:<14s}  {count:>4d} tasks{marker}")

    return lines


def render_org_breakdown(org_data: list[dict]) -> list[str]:
    """Render per-org mirror sync status breakdown."""
    lines = [hdr("PER-ORG MIRROR SYNC BREAKDOWN")]
    if not org_data:
        lines.append(info("No mirror configurations found."))
        return lines

    w_org = max((len(o["org"]) for o in org_data), default=10)
    w_org = max(w_org, 12)

    fmt = ("  {:<{w_org}s}  {:>5s}  {:>7s}  {:>7s}  {:>4s}"
           "  {:>8s}  {:>7s}  {:>7s}  {:>8s}")
    header = fmt.format(
        "ORGANIZATION", "TOTAL", "SUCCESS", "N.R.",  "FAIL",
        "SYNC_NOW", "SYNCING", "ENABLED", "DISABLED",
        w_org=w_org,
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))

    for o in org_data:
        in_flight = o["sync_now"] + o["syncing"]
        marker = ""
        if in_flight > 0:
            marker = (
                f"  {C_OK}<-- {o['sync_now']} queued, "
                f"{o['syncing']} running{C_RST}"
            )
        if o["fail"] > 0:
            pfx = ", " if marker else "  "
            marker += f"{pfx}{C_WRN}{o['fail']} failed{C_RST}"
        lines.append(fmt.format(
            o["org"],
            str(o["total"]),
            str(o["success"]),
            str(o["never_run"]),
            str(o["fail"]),
            str(o["sync_now"]),
            str(o["syncing"]),
            str(o["enabled"]),
            str(o["disabled"]),
            w_org=w_org,
        ) + marker)

    # Totals
    lines.append("  " + "-" * (len(header) - 2))
    lines.append(fmt.format(
        "TOTAL",
        str(sum(o["total"] for o in org_data)),
        str(sum(o["success"] for o in org_data)),
        str(sum(o["never_run"] for o in org_data)),
        str(sum(o["fail"] for o in org_data)),
        str(sum(o["sync_now"] for o in org_data)),
        str(sum(o["syncing"] for o in org_data)),
        str(sum(o["enabled"] for o in org_data)),
        str(sum(o["disabled"] for o in org_data)),
        w_org=w_org,
    ))

    return lines


DETAIL_LIMIT = 25


def _limited(tasks: list[dict], limit: int) -> tuple[list[dict], int]:
    if len(tasks) <= limit:
        return tasks, 0
    return tasks[:limit], len(tasks) - limit


def render_health(running_pods: int, total_pods: int,
                  stuck: list[dict], running: list[dict],
                  queued: list[dict]) -> list[str]:
    disabled_queued = sum(1 for task in queued if not task["enabled"])
    lines = [hdr("QUEUE HEALTH")]
    lines.append(
        f"  Mirror workers:  {running_pods} running / {total_pods} pods")
    lines.append(f"  Queued SYNC_NOW: {len(queued)}")
    lines.append(
        f"  Running SYNCING: {len(running)}  (claim still valid)")
    lines.append(
        f"  Stuck claims:    {len(stuck)}  "
        f"(SYNCING past expiration)")
    if queued and not stuck and running_pods and running:
        lines.append(ok(
            "SYNC_NOW rows are waiting for a free mirror worker. "
            "They are not stuck."))
    elif queued and not stuck and running_pods and not running:
        lines.append(warn(
            "SYNC_NOW is non-empty and nothing is SYNCING. "
            "Workers may be between repositories."))
    if running_pods == 0:
        lines.append(err(
            "No mirror worker is running. The queue cannot drain."))
    if stuck:
        lines.append(warn(
            f"{len(stuck)} SYNCING claim(s) expired. Quay does not "
            f"reclaim a disabled mirror from that state. "
            f"Put them back with --requeue-stuck."))
    if disabled_queued:
        lines.append(info(
            f"{disabled_queued} queued mirrors are disabled. "
            f"That is the regional on-demand pattern: the worker "
            f"claims them only from SYNC_NOW. NEVER_RUN would drop them."))
    return lines


def render_activity(activity: dict, seconds: int, queued: int) -> list[str]:
    who = ""
    if activity["enqueuers"]:
        who = "; requested by " + ", ".join(
            f"{item['user']} ({item['count']})"
            for item in activity["enqueuers"])
    lines = [(
        f"  Last {format_duration(seconds)}: "
        f"{activity['success']} repos finished, "
        f"{activity['failed']} failed, "
        f"{activity['tag_success']} tags copied, "
        f"{activity['enqueued']} set to SYNC_NOW{who}"
    )]
    if queued and activity["enqueued"] > activity["success"]:
        lines.append(warn(
            "The SYNC_NOW list is growing: new sync-now requests are "
            "ahead of finished repositories. Workers are still copying. "
            "This is a backlog, not an abandoned claim."))
    elif queued and activity["success"] > activity["enqueued"]:
        net = activity["success"] - activity["enqueued"]
        rate = net / max(seconds, 1)
        if rate > 0:
            eta = int(queued / rate)
            lines.append(ok(
                f"Queue is draining. At this net rate, about "
                f"{format_duration(eta)} to finish the current "
                f"SYNC_NOW list."))
    for failure in activity["failures"]:
        lines.append(
            f"    fail x{failure['count']}: {failure['message']}")
    return lines


def render_upstream(host_rows: list[dict], allowed: list[str],
                    codes: MirrorCodes) -> list[str]:
    if not allowed:
        return [info(
            "Could not read SSRF_ALLOWED_HOSTS; "
            "upstream host check skipped.")]
    by_host: dict[str, dict[str, int]] = {}
    for row in host_rows:
        code = parse_int(row["status"]) if row["status"] != "" else None
        label = "UNSET" if row["status"] == "" else codes.label(code)
        counts = by_host.setdefault(row["host"], {})
        counts[label] = counts.get(label, 0) + row["count"]
    blocked = []
    for host, counts in by_host.items():
        if host in allowed:
            continue
        interesting = (
            counts.get("FAIL", 0) + counts.get("SYNC_NOW", 0)
            + counts.get("SYNCING", 0))
        if interesting:
            blocked.append((host, counts))
    if not blocked:
        return [ok(
            "Queued and failed mirrors use an allowed upstream: "
            + ", ".join(allowed))]
    lines = [warn(
        "The mirror worker rejects these upstream hosts "
        "(not in SSRF_ALLOWED_HOSTS). A regional quay can pull only "
        "from its central quay's registry hostname:")]
    for host, counts in sorted(blocked, key=lambda item: -sum(item[1].values())):
        bits = ", ".join(
            f"{count} {label}" for label, count in sorted(counts.items())
            if count)
        lines.append(f"    {host}  ({bits})")
    lines.append(info("Allowed: " + ", ".join(allowed)))
    return lines


def render_report(
    hub_short: str, quay_ns: str, quay_cr: str, quay_ver: str,
    db_pod: str, quay_db: str, codes: MirrorCodes,
    summary: dict, org_data: list[dict],
    stuck: list[dict], running: list[dict], queued: list[dict],
    failed: list[dict], activity: dict, activity_window: int,
    workers: tuple[int, int], allowed_hosts: list[str],
    host_rows: list[dict], brief: bool,
) -> list[str]:
    lines = []
    lines.append(hdr(f"QUAY MIRROR SYNC STATUS \u2014 {hub_short}"))
    lines.append(info(f"Namespace:    {quay_ns}"))
    lines.append(info(f"QuayRegistry: {quay_cr}"))
    lines.append(info(f"Quay version: {quay_ver}"))
    lines.append(info(f"DB pod:       {db_pod}"))
    lines.append(info(f"DB name:      {quay_db}"))
    lines.append(info(
        f"Status map:   SYNCING={codes.syncing} "
        f"FAIL={codes.fail} SYNC_NOW={codes.sync_now} "
        f"({codes.source})"))

    lines.extend(render_health(
        workers[0], workers[1], stuck, running, queued))
    lines.extend(render_activity(activity, activity_window, len(queued)))
    if not brief:
        lines.extend(render_upstream(host_rows, allowed_hosts, codes))

    lines.append(hdr("MIRROR SYNC STATUS SUMMARY"))
    lines.extend(render_summary_table(summary))

    if brief:
        return lines

    lines.extend(render_org_breakdown(org_data))

    if running:
        lines.append(hdr(f"RUNNING SYNCS ({len(running)} tasks)"))
        lines.append("")
        shown, extra = _limited(running, DETAIL_LIMIT)
        lines.extend(render_task_table(shown, "RUNNING", "CLAIM"))
        if extra:
            lines.append(info(f"{extra} more running syncs not listed."))

    if stuck:
        lines.append(hdr(
            f"STUCK CLAIMS ({len(stuck)} tasks, "
            f"{C_ERR}worker will not resume these{C_RST}{C_HDR})"))
        lines.append("")
        shown, extra = _limited(stuck, DETAIL_LIMIT)
        lines.extend(render_task_table(shown, "STUCK", "CLAIM"))
        if extra:
            lines.append(info(f"{extra} more stuck claims not listed."))
        lines.append(info("Requeue them with --requeue-stuck."))

    if queued:
        lines.append(hdr(f"QUEUED SYNC_NOW ({len(queued)} tasks)"))
        lines.append("")
        shown, extra = _limited(queued, DETAIL_LIMIT)
        lines.extend(render_task_table(shown, "QUEUED", "QUEUED"))
        lines.extend(render_age_breakdown(queued))
        if extra:
            lines.append(info(
                f"{extra} more queued repositories not listed. "
                f"Oldest are shown first."))

    if failed:
        lines.append(hdr(f"FAILED SYNC TASKS ({len(failed)} tasks)"))
        lines.append("")
        shown, extra = _limited(failed, DETAIL_LIMIT)
        lines.extend(render_task_table(shown, "FAILED", "SINCE"))
        lines.extend(render_age_breakdown(failed))
        if extra:
            lines.append(info(f"{extra} more failed tasks not listed."))

    if not queued and not running and not stuck:
        lines.append("")
        lines.append(ok("No queued or running mirror syncs."))

    return lines


# ===================================================================
# Main
# ===================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Inspect the regional Quay mirror queue on an OpenShift hub "
            "and requeue claims the worker has abandoned."
        ),
        epilog=(
            "By default the script only prints a report.\n"
            "SYNC_NOW is the queue. Workers drain it. Do not reset those\n"
            "rows to NEVER_RUN: a disabled regional mirror is claimed\n"
            "only while its status is SYNC_NOW.\n"
            "\n"
            "  ./ocp-quay-sync-tasks.py\n"
            "  ./ocp-quay-sync-tasks.py --watch 30\n"
            "  ./ocp-quay-sync-tasks.py --requeue-stuck\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--watch",
        nargs="?",
        const=30,
        type=int,
        metavar="SECONDS",
        help=(
            "Poll every SECONDS (default 30) until no SYNC_NOW rows and "
            "no live SYNCING claims remain. Read-only."
        ),
    )
    parser.add_argument(
        "--requeue-stuck",
        action="store_true",
        default=False,
        help=(
            "Put expired SYNCING claims back on the queue. Disabled "
            "mirrors return to SYNC_NOW; enabled mirrors return to "
            "NEVER_RUN. Live claims are left alone."
        ),
    )
    parser.add_argument(
        "--cancel-all-running",
        action="store_true",
        default=False,
        help=(
            "Same as --requeue-stuck. Does not abort the SYNC_NOW queue "
            "or a copy that is still inside its claim window."
        ),
    )
    parser.add_argument(
        "--clean-failed-tasks",
        action="store_true",
        default=False,
        help=(
            "Retry FAIL rows. Disabled mirrors are set to SYNC_NOW so "
            "the worker claims them; enabled mirrors are set to "
            "NEVER_RUN. Hosts outside SSRF_ALLOWED_HOSTS will fail again."
        ),
    )
    parser.add_argument(
        "--clean-all",
        action="store_true",
        default=False,
        help=(
            "Requeue expired claims, retry FAIL rows, and repair NULL "
            "or unknown statuses. Does not touch the live queue or "
            "in-progress copies."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Skip the confirmation prompt on write actions.",
    )
    return parser.parse_args()


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"  {prompt} yes (--yes)")
        return True
    try:
        answer = input(f"  {C_BLD}{prompt} [y/N]:{C_RST} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n" + info("Aborted."))
        sys.exit(0)
    return answer in ("y", "yes")


def collect_snapshot(reg: dict, codes: MirrorCodes, window: int,
                     brief: bool = False) -> dict:
    quay_ns = reg["ns"]
    db_pod = reg["db_pod"]
    db_user = reg.get("db_user", "postgres")
    quay_db = reg["quay_db"]
    summary = get_sync_summary(quay_ns, db_pod, quay_db, db_user, codes)
    inflight = get_inflight(quay_ns, db_pod, quay_db, db_user, codes)
    stuck, running, queued = classify_inflight(inflight)
    if brief:
        org_data: list[dict] = []
        failed: list[dict] = []
        hosts: list[dict] = []
    else:
        org_data = get_org_breakdown(
            quay_ns, db_pod, quay_db, db_user, codes)
        failed = get_failed_syncs(
            quay_ns, db_pod, quay_db, db_user, codes)
        hosts = upstream_hosts(quay_ns, db_pod, quay_db, db_user)
    return {
        "summary": summary,
        "org_data": org_data,
        "stuck": stuck,
        "running": running,
        "queued": queued,
        "failed": failed,
        "activity": recent_mirror_activity(
            quay_ns, db_pod, quay_db, db_user, window),
        "workers": mirror_worker_counts(quay_ns),
        "hosts": hosts,
        "unset": summary.get("UNSET", 0),
    }


def process_registry(
    args, hub_short: str, reg: dict, multi: bool,
):
    """Process a single regional QuayRegistry: report, requeue, watch."""
    quay_ns = reg["ns"]
    quay_cr = reg["cr"]
    quay_ver = reg["ver"]
    db_pod = reg["db_pod"]
    db_user = reg.get("db_user", "postgres")
    quay_db = reg["quay_db"]
    codes = detect_status_map(quay_ns)
    allowed = ssrf_allowed_hosts(quay_ns)
    reg_label = (f" [{quay_ns}/{quay_cr}]" if multi else "")

    if args.watch is not None and args.watch < 5:
        print(err("--watch interval must be at least 5 seconds."))
        sys.exit(2)
    report_window = 600
    poll_window = args.watch if args.watch is not None else report_window

    want_requeue = (
        args.requeue_stuck or args.cancel_all_running or args.clean_all)
    want_clean = args.clean_failed_tasks or args.clean_all
    want_null = args.clean_all

    def show(snap: dict, brief: bool, activity_window: int):
        report = render_report(
            hub_short, quay_ns, quay_cr, quay_ver,
            db_pod, quay_db, codes,
            snap["summary"], snap["org_data"],
            snap["stuck"], snap["running"], snap["queued"],
            snap["failed"], snap["activity"], activity_window,
            snap["workers"], allowed, snap["hosts"], brief,
        )
        print("\n".join(report))

    snap = collect_snapshot(reg, codes, report_window)
    show(snap, False, report_window)

    if want_requeue:
        print("")
        print(hdr(f"ACTION: REQUEUE EXPIRED CLAIMS{reg_label}"))
        print(
            "  SYNCING rows whose 12h claim has expired go back on the "
            "queue.")
        print(
            "  Disabled mirrors are set to SYNC_NOW. Enabled mirrors "
            "are set to NEVER_RUN.")
        print(
            "  Live copies and the SYNC_NOW backlog are not touched.")
        print("")
        if not snap["stuck"]:
            print(ok("No expired claims. Nothing to requeue."))
        elif confirm(
                f"Requeue {len(snap['stuck'])} expired claims?", args.yes):
            affected = requeue_where(
                quay_ns, db_pod, quay_db, db_user, codes,
                stuck_claim_where(codes))
            if affected:
                print(ok(f"Requeued {affected} expired claims."))
            else:
                print(warn("No rows affected."))
        else:
            print(info("Skipped requeue."))

    if want_clean:
        print("")
        print(hdr(f"ACTION: RETRY FAILED SYNCS{reg_label}"))
        print(
            f"  {len(snap['failed'])} FAIL rows will be claimed again. "
            f"Disabled mirrors become SYNC_NOW.")
        print(
            "  An upstream host that is not in SSRF_ALLOWED_HOSTS "
            "will fail again.")
        print("")
        if not snap["failed"]:
            print(ok("No failed syncs."))
        elif confirm(
                f"Retry {len(snap['failed'])} failed syncs?", args.yes):
            affected = requeue_where(
                quay_ns, db_pod, quay_db, db_user, codes,
                f"sync_status = {codes.fail}")
            if affected:
                print(ok(f"Requeued {affected} failed syncs."))
            else:
                print(warn("No rows affected."))
        else:
            print(info("Skipped failed syncs."))

    if want_null:
        known = ", ".join(str(code) for code in sorted(codes.known))
        where = (
            f"sync_status IS NULL OR sync_status NOT IN ({known})")
        print("")
        print(hdr(f"ACTION: REPAIR UNSET STATUSES{reg_label}"))
        if snap["unset"] == 0:
            print(ok("No NULL sync_status rows."))
        elif confirm(
                f"Repair {snap['unset']} unset sync rows?", args.yes):
            affected = requeue_where(
                quay_ns, db_pod, quay_db, db_user, codes, where)
            if affected:
                print(ok(f"Repaired {affected} unset rows."))
            else:
                print(warn("No rows affected."))
        else:
            print(info("Skipped unset rows."))

    if want_requeue or want_clean or want_null:
        snap = collect_snapshot(reg, codes, report_window)
        print("")
        print(hdr(f"UPDATED MIRROR SYNC STATUS{reg_label}"))
        for line in render_summary_table(snap["summary"]):
            print(line)
        print("")
        for line in render_health(
                snap["workers"][0], snap["workers"][1],
                snap["stuck"], snap["running"], snap["queued"]):
            print(line)

    if not (want_requeue or want_clean or want_null) and args.watch is None:
        print("")
        if snap["stuck"]:
            print(info(
                f"Expired claims: {C_BLD}--requeue-stuck{C_RST}"))
        if snap["queued"] or snap["running"]:
            print(info(
                f"Watch until the queue drains: {C_BLD}--watch 30{C_RST}"))
        if snap["failed"]:
            print(info(
                f"Retry failures: {C_BLD}--clean-failed-tasks{C_RST}"))
        return

    if args.watch is None:
        return

    print("")
    print(info(
        f"Watching every {poll_window}s. Stops when SYNC_NOW and live "
        f"SYNCING are both empty. Ctrl-C leaves the queue as it is."))
    try:
        while True:
            if (not snap["queued"] and not snap["running"]
                    and not snap["stuck"]):
                print(ok("Mirror queue is empty."))
                return
            if snap["stuck"] and not snap["queued"] and not snap["running"]:
                print(warn(
                    "Only expired claims remain. Re-run with "
                    "--requeue-stuck."))
                sys.exit(2)
            time.sleep(poll_window)
            snap = collect_snapshot(
                reg, codes, poll_window, brief=True)
            show(snap, True, poll_window)
    except KeyboardInterrupt:
        print("\n" + info("Watch stopped. The queue was not modified."))


def main():
    args = parse_args()

    # Pre-flight
    hub_api, hub_fqdn, hub_short = preflight()
    print(hdr("QUAY MIRROR SYNC"))
    print(f"  Cluster:          {hub_short}")
    print(info(f"Connected to: {hub_short} ({hub_api})"))

    # Discover all QuayRegistry instances
    registries = discover_quay_registries()
    multi = len(registries) > 1

    if multi:
        print(info(f"Found {len(registries)} Quay registries:"))
        for reg in registries:
            print(info(
                f"  {reg['ns']}/{reg['cr']} "
                f"(Quay {reg['ver']})"))
    else:
        reg = registries[0]
        print(info(
            f"Found Quay {reg['ver']} in namespace '{reg['ns']}'"))

    # Process each registry
    for reg in registries:
        process_registry(args, hub_short, reg, multi)

    print("")
    print(info(
        f"Done at "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"))


if __name__ == "__main__":
    main()
