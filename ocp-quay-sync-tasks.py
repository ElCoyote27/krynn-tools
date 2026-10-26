#!/usr/bin/env python3
from __future__ import annotations
# ocp-quay-sync-tasks.py v1.01
#
# Inspect and cancel stuck Quay mirror sync tasks on OpenShift clusters.
#
# On regional quays (hosted on OCP), mirror sync tasks frequently get
# stuck in SYNCING or SYNC_NOW state for weeks or months, preventing
# further updates.  By default this script is READ-ONLY and shows a
# report.  Pass --cancel-all to reset stuck tasks after confirmation.
#
# Workflow:
#   1. Discovers the Quay registry (namespace, DB pod, version)
#   2. Lists all mirror sync tasks and their current state
#   3. Highlights stuck tasks (SYNCING / SYNC_NOW) with age
#   4. With action flags: asks for confirmation, then resets
#      tasks to NEVER_RUN so the mirror worker picks them up fresh
#
# Supports:
#   - Quay 3.15 and 3.16 (auto-detected)
#   - Both "quay" and "quay-enterprise" namespaces
#
# Usage:
#   export KUBECONFIG=/path/to/your-cluster-kubeconfig
#
#   # Read-only report (default):
#   ./ocp-quay-sync-tasks.py
#
#   # Cancel all stuck tasks (SYNCING/SYNC_NOW):
#   ./ocp-quay-sync-tasks.py --cancel-all-running
#
#   # Reset all failed tasks (FAIL):
#   ./ocp-quay-sync-tasks.py --clean-failed-tasks
#
#   # Both at once:
#   ./ocp-quay-sync-tasks.py --cancel-all-running --clean-failed-tasks
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
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Sync status values (from Quay data model)
# ---------------------------------------------------------------------------
SYNC_STATUS = {
    -1: "UNSET",       # NULL or -1 in database (invalid/uninitialized)
    0:  "NEVER_RUN",
    1:  "SUCCESS",
    2:  "FAIL",
    3:  "SYNC_NOW",
    4:  "SYNCING",
}

STUCK_STATES = (3, 4)   # SYNC_NOW, SYNCING


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


def age_str(dt_str: str) -> str:
    """Return a human-readable age string from an ISO timestamp."""
    if not dt_str or dt_str.strip() == "":
        return "unknown"
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        total_secs = int(delta.total_seconds())
        if total_secs < 0:
            return "scheduled"
        days = delta.days
        if days > 365:
            return f"{days // 365}y {(days % 365) // 30}mo"
        if days > 30:
            return f"{days // 30}mo {days % 30}d"
        if days > 0:
            return f"{days}d {delta.seconds // 3600}h"
        hours = delta.seconds // 3600
        mins = (delta.seconds % 3600) // 60
        if hours > 0:
            return f"{hours}h {mins}m"
        secs = delta.seconds % 60
        if mins > 0:
            return f"{mins}m {secs}s"
        return f"{secs}s"
    except (ValueError, TypeError):
        return "unknown"


# Thresholds for flagging tasks as likely stuck
WARN_THRESHOLD_SECS = 3600       # 1 hour
STUCK_THRESHOLD_SECS = 86400     # 1 day


def age_seconds(dt_str: str) -> int | None:
    """Return age in seconds, or None if unparseable."""
    if not dt_str or dt_str.strip() == "":
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - dt).total_seconds())
    except (ValueError, TypeError):
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


# ===================================================================
# Query sync tasks
# ===================================================================

def get_stuck_syncs(quay_ns: str, db_pod: str, quay_db: str,
                    db_user: str = "postgres") -> list[dict]:
    """Query the database for all mirror configs in SYNC_NOW or SYNCING state."""
    progress("Querying stuck sync tasks...")

    sql = """
        SELECT u.username,
               r.name,
               rmc.sync_status,
               rmc.sync_start_date,
               rmc.external_reference,
               rmc.sync_retries_remaining,
               rmc.is_enabled,
               rmc.id
        FROM repomirrorconfig rmc
        JOIN repository r ON rmc.repository_id = r.id
        JOIN "user" u ON r.namespace_user_id = u.id
        WHERE rmc.sync_status IN (3, 4)
        ORDER BY rmc.sync_start_date ASC NULLS FIRST;
    """

    raw = quay_psql(quay_ns, db_pod, quay_db, sql, db_user)
    progress_clear()

    if not raw or not raw.strip():
        return []

    stuck = []
    for line in raw.strip().splitlines():
        parts = line.split("|")
        if len(parts) >= 8:
            status_int = int(parts[2]) if parts[2].isdigit() else -1
            stuck.append({
                "org":        parts[0],
                "repo":       parts[1],
                "status":     SYNC_STATUS.get(status_int, parts[2]),
                "status_int": status_int,
                "start_date": parts[3],
                "ext_ref":    parts[4],
                "retries":    parts[5],
                "enabled":    parts[6],
                "id":         parts[7],
            })

    return stuck


def get_sync_summary(quay_ns: str, db_pod: str, quay_db: str,
                     db_user: str = "postgres") -> dict:
    """Get a count of all mirror configs by status."""
    sql = """
        SELECT coalesce(rmc.sync_status, -1), count(*)
        FROM repomirrorconfig rmc
        GROUP BY coalesce(rmc.sync_status, -1)
        ORDER BY coalesce(rmc.sync_status, -1);
    """
    raw = quay_psql(quay_ns, db_pod, quay_db, sql, db_user)
    summary = {}
    if raw:
        for line in raw.strip().splitlines():
            parts = line.split("|")
            if len(parts) >= 2:
                status_int = int(parts[0]) if parts[0].isdigit() else -1
                count = int(parts[1]) if parts[1].isdigit() else 0
                label = SYNC_STATUS.get(status_int, f"UNKNOWN({parts[0]})")
                summary[label] = count
    return summary


def get_failed_syncs(quay_ns: str, db_pod: str, quay_db: str,
                     db_user: str = "postgres") -> list[dict]:
    """Query the database for all mirror configs in FAIL state."""
    progress("Querying failed sync tasks...")

    sql = """
        SELECT u.username,
               r.name,
               rmc.sync_status,
               rmc.sync_start_date,
               rmc.external_reference,
               rmc.sync_retries_remaining,
               rmc.is_enabled,
               rmc.id
        FROM repomirrorconfig rmc
        JOIN repository r ON rmc.repository_id = r.id
        JOIN "user" u ON r.namespace_user_id = u.id
        WHERE rmc.sync_status = 2
        ORDER BY u.username, r.name;
    """

    raw = quay_psql(quay_ns, db_pod, quay_db, sql, db_user)
    progress_clear()

    if not raw or not raw.strip():
        return []

    failed = []
    for line in raw.strip().splitlines():
        parts = line.split("|")
        if len(parts) >= 8:
            status_int = int(parts[2]) if parts[2].isdigit() else -1
            failed.append({
                "org":        parts[0],
                "repo":       parts[1],
                "status":     SYNC_STATUS.get(status_int, parts[2]),
                "status_int": status_int,
                "start_date": parts[3],
                "ext_ref":    parts[4],
                "retries":    parts[5],
                "enabled":    parts[6],
                "id":         parts[7],
            })

    return failed


def get_unset_count(quay_ns: str, db_pod: str, quay_db: str,
                    db_user: str = "postgres") -> int:
    """Count mirror configs with NULL sync_status."""
    sql = """
        SELECT count(*)
        FROM repomirrorconfig
        WHERE sync_status IS NULL;
    """
    raw = quay_psql(quay_ns, db_pod, quay_db, sql, db_user)
    if raw and raw.strip().isdigit():
        return int(raw.strip())
    return 0


def reset_null_syncs(
    quay_ns: str, db_pod: str, quay_db: str,
    db_user: str = "postgres",
) -> int:
    """Reset NULL or invalid sync_status to NEVER_RUN (0). Returns affected rows."""
    sql = """
        UPDATE repomirrorconfig
        SET sync_status = 0,
            sync_retries_remaining = 3,
            sync_start_date = now()
        WHERE sync_status IS NULL
           OR sync_status NOT IN (0, 1, 2, 3, 4);
    """
    out = run([
        "oc", "exec", db_pod, "-n", quay_ns, "--",
        "psql", "-U", db_user, "-d", quay_db,
        "-c", sql,
    ])
    m = re.search(r"UPDATE\s+(\d+)", out)
    return int(m.group(1)) if m else 0


def get_org_breakdown(quay_ns: str, db_pod: str, quay_db: str,
                      db_user: str = "postgres") -> list[dict]:
    """Get per-org mirror sync status breakdown."""
    progress("Querying per-org mirror breakdown...")

    sql = """
        SELECT u.username,
               count(*) AS total,
               count(*) FILTER (WHERE rmc.sync_status = 0) AS never_run,
               count(*) FILTER (WHERE rmc.sync_status = 1) AS success,
               count(*) FILTER (WHERE rmc.sync_status = 2) AS fail,
               count(*) FILTER (WHERE rmc.sync_status = 3) AS sync_now,
               count(*) FILTER (WHERE rmc.sync_status = 4) AS syncing,
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

    if not raw or not raw.strip():
        return []

    orgs = []
    for line in raw.strip().splitlines():
        parts = line.split("|")
        if len(parts) >= 9:
            orgs.append({
                "org":       parts[0],
                "total":     int(parts[1]) if parts[1].isdigit() else 0,
                "never_run": int(parts[2]) if parts[2].isdigit() else 0,
                "success":   int(parts[3]) if parts[3].isdigit() else 0,
                "fail":      int(parts[4]) if parts[4].isdigit() else 0,
                "sync_now":  int(parts[5]) if parts[5].isdigit() else 0,
                "syncing":   int(parts[6]) if parts[6].isdigit() else 0,
                "enabled":   int(parts[7]) if parts[7].isdigit() else 0,
                "disabled":  int(parts[8]) if parts[8].isdigit() else 0,
            })

    return orgs


# ===================================================================
# Cancel stuck syncs
# ===================================================================

def reset_syncs(
    quay_ns: str, db_pod: str, quay_db: str,
    states: tuple[int, ...],
    db_user: str = "postgres",
) -> int:
    """Reset mirror configs in given states to NEVER_RUN. Returns affected rows."""
    placeholders = ", ".join(str(s) for s in states)
    sql = f"""
        UPDATE repomirrorconfig
        SET sync_status = 0,
            sync_retries_remaining = 3,
            sync_start_date = now()
        WHERE sync_status IN ({placeholders});
    """
    out = run([
        "oc", "exec", db_pod, "-n", quay_ns, "--",
        "psql", "-U", db_user, "-d", quay_db,
        "-c", sql,
    ])
    m = re.search(r"UPDATE\s+(\d+)", out)
    return int(m.group(1)) if m else 0


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
    for status_name in ("NEVER_RUN", "SUCCESS", "FAIL",
                        "SYNC_NOW", "SYNCING", "UNSET"):
        count = summary.get(status_name, 0)
        if count == 0 and status_name == "UNSET":
            continue
        marker = ""
        if status_name in ("SYNC_NOW", "SYNCING") and count > 0:
            marker = f"  {C_OK}<-- in flight{C_RST}"
        if status_name == "FAIL" and count > 0:
            marker = f"  {C_WRN}<-- check below{C_RST}"
        if status_name == "UNSET" and count > 0:
            marker = f"  {C_DIM}(NULL or invalid in DB){C_RST}"
        lines.append(fmt.format(status_name, str(count)) + marker)
    for status_name, count in summary.items():
        if status_name not in ("NEVER_RUN", "SUCCESS", "FAIL",
                               "SYNC_NOW", "SYNCING", "UNSET"):
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
            age = age_str(s["start_date"])
            secs = age_seconds(s["start_date"])
            marker = ""
            if secs is not None and secs > STUCK_THRESHOLD_SECS:
                marker = f"  {C_ERR}<-- likely stuck{C_RST}"
            elif secs is not None and secs > WARN_THRESHOLD_SECS:
                marker = f"  {C_WRN}<-- slow?{C_RST}"
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
        try:
            dt = datetime.fromisoformat(
                s["start_date"].replace("Z", "+00:00"))
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
        except (ValueError, TypeError):
            buckets["unknown"] += 1

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
            marker = f"  {C_OK}<-- {in_flight} in flight{C_RST}"
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


def render_report(
    hub_short: str, quay_ns: str, quay_cr: str, quay_ver: str,
    db_pod: str, quay_db: str,
    summary: dict, org_data: list[dict],
    stuck: list[dict], failed: list[dict],
) -> list[str]:
    lines = []

    # Quay info
    lines.append(hdr(f"QUAY MIRROR SYNC STATUS \u2014 {hub_short}"))
    lines.append(info(f"Namespace:    {quay_ns}"))
    lines.append(info(f"QuayRegistry: {quay_cr}"))
    lines.append(info(f"Quay version: {quay_ver}"))
    lines.append(info(f"DB pod:       {db_pod}"))
    lines.append(info(f"DB name:      {quay_db}"))

    # Summary
    lines.append(hdr("MIRROR SYNC STATUS SUMMARY"))
    lines.extend(render_summary_table(summary))

    # Per-org breakdown
    lines.extend(render_org_breakdown(org_data))

    # Failed task details
    if failed:
        lines.append(hdr(f"FAILED SYNC TASKS ({len(failed)} tasks)"))
        lines.append("")
        lines.extend(render_task_table(failed, "FAILED", "SINCE"))
        lines.extend(render_age_breakdown(failed))

    # In-flight task details
    if not stuck:
        lines.append("")
        lines.append(ok("No in-flight sync tasks found."))
        return lines

    # Classify: how many are likely stuck vs legitimately running
    n_stuck = 0
    for s in stuck:
        secs = age_seconds(s["start_date"])
        if secs is not None and secs > STUCK_THRESHOLD_SECS:
            n_stuck += 1

    if n_stuck > 0:
        lines.append(hdr(
            f"IN-FLIGHT SYNC TASKS ({len(stuck)} tasks, "
            f"{C_ERR}{n_stuck} likely stuck{C_RST}{C_HDR})"))
    else:
        lines.append(hdr(f"IN-FLIGHT SYNC TASKS ({len(stuck)} tasks)"))
    lines.append("")
    lines.extend(render_task_table(stuck, "IN-FLIGHT", "RUNNING"))
    lines.extend(render_age_breakdown(stuck))

    return lines


# ===================================================================
# Main
# ===================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Inspect and cancel stuck Quay mirror sync tasks "
            "on OpenShift clusters."
        ),
        epilog=(
            "By default, the script only shows a read-only report.\n"
            "Pass --cancel-all-running and/or --clean-failed-tasks\n"
            "to reset tasks after confirmation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--cancel-all-running",
        action="store_true",
        default=False,
        help=(
            "Cancel ALL stuck sync tasks (SYNCING and SYNC_NOW) by "
            "resetting them to NEVER_RUN.  Asks for confirmation first."
        ),
    )
    parser.add_argument(
        "--clean-failed-tasks",
        action="store_true",
        default=False,
        help=(
            "Reset ALL failed sync tasks (FAIL) to NEVER_RUN so the "
            "mirror worker retries them.  Asks for confirmation first."
        ),
    )
    parser.add_argument(
        "--clean-all",
        action="store_true",
        default=False,
        help=(
            "Reset ALL non-successful sync tasks (SYNCING, SYNC_NOW, "
            "FAIL, and NULL/UNSET) to NEVER_RUN.  Asks for confirmation "
            "first.  Equivalent to --cancel-all-running + "
            "--clean-failed-tasks + NULL cleanup."
        ),
    )
    return parser.parse_args()


def process_registry(
    args, hub_short: str, reg: dict, multi: bool,
):
    """Process a single QuayRegistry: report and optionally act."""
    quay_ns = reg["ns"]
    quay_cr = reg["cr"]
    quay_ver = reg["ver"]
    db_pod = reg["db_pod"]
    db_user = reg.get("db_user", "postgres")
    quay_db = reg["quay_db"]

    # Gather data
    summary = get_sync_summary(quay_ns, db_pod, quay_db, db_user)
    org_data = get_org_breakdown(quay_ns, db_pod, quay_db, db_user)
    stuck = get_stuck_syncs(quay_ns, db_pod, quay_db, db_user)
    failed = get_failed_syncs(quay_ns, db_pod, quay_db, db_user)
    unset_count = summary.get("UNSET", 0)

    # Render report (always shown)
    report = render_report(
        hub_short, quay_ns, quay_cr, quay_ver,
        db_pod, quay_db, summary, org_data, stuck, failed,
    )
    print("\n".join(report))

    # --clean-all implies everything
    want_cancel = args.cancel_all_running or args.clean_all
    want_clean = args.clean_failed_tasks or args.clean_all
    want_all = args.clean_all

    # Hints for available actions
    if not want_cancel and not want_clean:
        if stuck or failed or unset_count > 0:
            print("")
            hints = []
        if stuck:
            hints.append(
                f"To cancel {len(stuck)} in-flight tasks: "
                f"{C_BLD}--cancel-all-running{C_RST}")
            if failed:
                hints.append(
                    f"To reset {len(failed)} failed tasks: "
                    f"{C_BLD}--clean-failed-tasks{C_RST}")
            if unset_count > 0:
                hints.append(
                    f"To reset {unset_count} unset (NULL) tasks: "
                    f"{C_BLD}--clean-all{C_RST}")
            if len(hints) > 1:
                hints.append(
                    f"To do everything at once: "
                    f"{C_BLD}--clean-all{C_RST}")
            for h in hints:
                print(info(h))
        return

    reg_label = (f" [{quay_ns}/{quay_cr}]" if multi else "")

    # --cancel-all-running
    if want_cancel and stuck:
        print("")
        print(hdr(f"ACTION: CANCEL ALL STUCK SYNC TASKS{reg_label}"))
        print(f"  This will reset {C_BLD}{len(stuck)}{C_RST} stuck sync "
              f"tasks (SYNCING/SYNC_NOW) to NEVER_RUN.")
        print(f"  The mirror worker will re-evaluate them on the next cycle.")
        print(f"  (sync_retries_remaining will also be reset to 3)")
        print("")
        try:
            answer = input(
                f"  {C_BLD}Cancel all {len(stuck)} stuck sync tasks? "
                f"[y/N]:{C_RST} "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n" + info("Aborted."))
            sys.exit(0)
        if answer in ("y", "yes"):
            progress("Cancelling stuck sync tasks...")
            affected = reset_syncs(quay_ns, db_pod, quay_db, (3, 4), db_user)
            progress_clear()
            if affected > 0:
                print(ok(f"Reset {affected} stuck tasks to NEVER_RUN."))
            else:
                print(warn("No rows affected \u2014 tasks may have "
                            "already changed state."))
        else:
            print(info("Skipped cancelling stuck tasks."))
    elif want_cancel and not stuck and not want_all:
        print("")
        print(ok("No in-flight sync tasks found. Nothing to cancel."))

    # --clean-failed-tasks
    if want_clean and failed:
        print("")
        print(hdr(f"ACTION: RESET ALL FAILED SYNC TASKS{reg_label}"))
        print(f"  This will reset {C_BLD}{len(failed)}{C_RST} failed sync "
              f"tasks (FAIL) to NEVER_RUN.")
        print(f"  The mirror worker will retry them on the next cycle.")
        print(f"  (sync_retries_remaining will also be reset to 3)")
        print("")
        try:
            answer = input(
                f"  {C_BLD}Reset all {len(failed)} failed sync tasks? "
                f"[y/N]:{C_RST} "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n" + info("Aborted."))
            sys.exit(0)
        if answer in ("y", "yes"):
            progress("Resetting failed sync tasks...")
            affected = reset_syncs(quay_ns, db_pod, quay_db, (2,), db_user)
            progress_clear()
            if affected > 0:
                print(ok(f"Reset {affected} failed tasks to NEVER_RUN."))
            else:
                print(warn("No rows affected \u2014 tasks may have "
                            "already changed state."))
        else:
            print(info("Skipped resetting failed tasks."))

    # --clean-all: also clean NULL/UNSET entries
    if want_all and unset_count > 0:
        print("")
        print(hdr(f"ACTION: RESET UNSET (NULL) SYNC TASKS{reg_label}"))
        print(f"  This will reset {C_BLD}{unset_count}{C_RST} tasks with "
              f"NULL sync_status to NEVER_RUN.")
        print("")
        try:
            answer = input(
                f"  {C_BLD}Reset all {unset_count} unset sync tasks? "
                f"[y/N]:{C_RST} "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n" + info("Aborted."))
            sys.exit(0)
        if answer in ("y", "yes"):
            progress("Resetting unset sync tasks...")
            affected = reset_null_syncs(quay_ns, db_pod, quay_db, db_user)
            progress_clear()
            if affected > 0:
                print(ok(f"Reset {affected} unset tasks to NEVER_RUN."))
            else:
                print(warn("No rows affected."))
        else:
            print(info("Skipped resetting unset tasks."))
    elif want_all and unset_count == 0:
        print("")
        print(ok("No unset (NULL) sync tasks found."))

    # Show updated summary after any action
    if want_cancel or want_clean:
        print("")
        new_summary = get_sync_summary(quay_ns, db_pod, quay_db, db_user)
        print(hdr(f"UPDATED MIRROR SYNC STATUS{reg_label}"))
        for line in render_summary_table(new_summary):
            print(line)


def main():
    args = parse_args()

    # Pre-flight
    hub_api, hub_fqdn, hub_short = preflight()
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
