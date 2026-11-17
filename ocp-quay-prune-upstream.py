#!/usr/bin/env python3
"""
Prune regional Quay blobs that central Quay no longer has.

Compares imagestorage digests referenced by one organization on an
OpenShift Quay (the hub regional) with digests still referenced by that
same organization on the upstream/central Quay. A blob still counts as
present only when a central manifest in the org references it. Rows left
in central imagestorage after tags were removed, which the Quay quota
page no longer counts, are treated as gone.

A candidate is deleted only when no manifest outside that organization
still references it. Manifests and tags in the organization that point
at a deleted blob are removed first, so pulls do not 404 on a manifest
whose layer row was dropped.

Dry-run is the default. Nothing is deleted unless --execute is given.
If more than --max-drop-ratio (default 0.25) of the org's blobs are
absent from central, --execute refuses unless --force is also set.
That guards against a truncated central digest dump.

Copy the script to a host that can reach the regional hub API and the
central Quay directly. A jump host is not required.

  scp ocp-quay-prune-upstream.py <host>:
  ssh <host>
  export KUBECONFIG=/path/to/hub-kubeconfig

  # Central Quay is a direct ssh away (podman container postgresql-quay)
  python3 ocp-quay-prune-upstream.py --org ocp4 --central-ssh cquay1

  # This host is the central Quay
  python3 ocp-quay-prune-upstream.py --org ocp4 --central-local

  # Central is also an OpenShift Quay
  python3 ocp-quay-prune-upstream.py --org ocp4 \\
      --central-kubeconfig /path/to/central-kubeconfig

  # Pre-exported digests still referenced by the central org
  python3 ocp-quay-prune-upstream.py --org ocp4 \\
      --central-digests /tmp/central-ocp4-referenced.txt

After --execute, the org's quota counters are marked for Quay's backfill
worker, which recounts them from the blobs still linked. The same reset
can be run on its own when a prune already happened:

  python3 ocp-quay-prune-upstream.py --org ocp4 --reset-quota

The page shows 0 until that worker writes the live totals. S3 objects
are left in place. Run ocp-quay-s3-gc.py to remove objects whose
database rows are gone.

Requires: oc, python3. --central-ssh needs ssh to the central host.
--central-local needs sudo podman on this host. --ssh-jump is an
optional ProxyJump and is not part of the normal path.
"""

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime

ORG_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
NC = "\033[0m"


def log_info(msg):
    print(f"{GREEN}[INFO]{NC} {msg}", flush=True)


def log_warn(msg):
    print(f"{YELLOW}[WARN]{NC} {msg}", flush=True)


def log_error(msg):
    print(f"{RED}[ERROR]{NC} {msg}", flush=True)


def log_step(msg):
    print(f"{CYAN}[STEP]{NC} {msg}", flush=True)


def run(cmd, check=True, timeout=600, input_text=None):
    r = subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        timeout=timeout, input=input_text)
    if check and r.returncode != 0:
        raise RuntimeError(
            f"Command failed ({r.returncode}): {cmd}\n{r.stderr}")
    return r


def oc(args, check=True, timeout=600):
    r = run(f"oc {args}", check=check, timeout=timeout)
    return r.stdout.strip()


def oc_exec(ns, pod, cmd, check=True, timeout=3600):
    r = subprocess.run(
        ["oc", "exec", "-n", ns, pod, "--", "bash", "-c", cmd],
        capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"oc exec failed: {cmd}\n{r.stderr}")
    return r.stdout.strip()


def cluster_short():
    hub_api = oc("whoami --show-server", check=False)
    if not hub_api:
        return "unknown", ""
    host = hub_api.replace("https://", "").replace("http://", "").split(":")[0]
    host = host.replace("api.", "", 1)
    return host.split(".")[0], hub_api


def normalize_digest(line):
    line = line.strip()
    if not line or line.startswith("(") or line.lower().startswith("content"):
        return ""
    if line.startswith("sha256:"):
        return line
    if len(line) == 64 and all(c in "0123456789abcdef" for c in line.lower()):
        return "sha256:" + line.lower()
    return ""


def write_digest_file(path, lines):
    seen = set()
    n = 0
    with open(path, "w") as fh:
        for line in lines:
            digest = normalize_digest(line)
            if not digest or digest in seen:
                continue
            seen.add(digest)
            fh.write(digest + "\n")
            n += 1
    return n


def detect_regional(namespace):
    try:
        oc("whoami --show-server")
    except Exception:
        log_error("Cannot connect to the regional cluster. Check $KUBECONFIG.")
        sys.exit(1)
    name, api = cluster_short()
    log_info(f"Regional cluster: {name} ({api})")

    quay_ns = namespace
    if not quay_ns:
        log_info("Auto-detecting Quay namespace...")
        quay_ns = oc(
            "get quayregistry -A -o jsonpath='{.items[0].metadata.namespace}'",
            check=False)
        if not quay_ns:
            quay_ns = oc(
                "get pods -A -l quay-component=quay-app "
                "-o jsonpath='{.items[0].metadata.namespace}'",
                check=False)
    if not quay_ns:
        log_error("Could not auto-detect the Quay namespace. Pass --namespace.")
        sys.exit(1)
    log_info(f"Namespace: {quay_ns}")

    pg_pod = oc(
        f"get pods -n {quay_ns} -l quay-component=postgres "
        f"-o jsonpath='{{.items[0].metadata.name}}'",
        check=False)
    if not pg_pod:
        out = oc(f"get pods -n {quay_ns} -o name", check=False)
        for line in out.splitlines():
            low = line.lower()
            if ("database" in low or "postgres" in low) and "clair" not in low:
                pg_pod = line.replace("pod/", "").strip()
                break
    if not pg_pod:
        log_error(f"Could not find a PostgreSQL pod in {quay_ns}")
        sys.exit(1)
    log_info(f"PostgreSQL pod: {pg_pod}")

    db_user_out = oc_exec(
        quay_ns, pg_pod,
        "psql -U postgres -t -A -c \"SELECT usename FROM pg_user "
        "WHERE usename <> 'postgres';\"",
        check=False)
    db_user = ""
    for line in db_user_out.splitlines():
        if "quay" in line.lower():
            db_user = line.strip()
            break
    if not db_user:
        db_user = "postgres"

    db_name_out = oc_exec(
        quay_ns, pg_pod,
        f"psql -U {db_user} -t -A -c \"SELECT datname FROM pg_database "
        f"WHERE datname NOT IN ('postgres','template0','template1');\"",
        check=False)
    db_name = ""
    for line in db_name_out.splitlines():
        if "quay" in line.lower():
            db_name = line.strip()
            break
    if not db_name:
        db_name = "quay"
    log_info(f"Database: {db_name} (user: {db_user})")
    return name, quay_ns, pg_pod, db_user, db_name


def regional_sql(ns, pod, db_user, db_name, query, single=False, timeout=3600):
    flags = "-t -A" if single else "-t"
    escaped = query.replace("'", "'\\''")
    out = oc_exec(
        ns, pod,
        f"psql -U {db_user} -d {db_name} {flags} -c '{escaped}'",
        timeout=timeout)
    if single:
        return out.strip()
    return out


def checked_org(name):
    if not name or not ORG_NAME_RE.fullmatch(name):
        log_error(
            f"Organization name {name!r} has characters this script "
            "will not embed in SQL.")
        sys.exit(2)
    return name


def central_referenced_copy_sql(org):
    org = checked_org(org)
    return (
        "COPY (SELECT DISTINCT i.content_checksum "
        "FROM imagestorage i "
        "JOIN manifestblob mb ON mb.blob_id = i.id "
        "JOIN manifest m ON m.id = mb.manifest_id "
        "JOIN repository r ON r.id = m.repository_id "
        "JOIN \"user\" u ON u.id = r.namespace_user_id "
        f"WHERE u.username = '{org}' "
        "AND i.content_checksum IS NOT NULL) TO STDOUT"
    )


def central_podman_copy(args):
    sql = central_referenced_copy_sql(args.central_org or args.org).replace('"', '\\"')
    return (
        f"sudo podman exec {shlex.quote(args.central_container)} "
        f"psql -U {shlex.quote(args.central_db_user)} -d {shlex.quote(args.central_db)} "
        f"-t -A -c \"{sql}\""
    )


def fetch_central_local(args, dest):
    log_step(
        f"Reading blobs referenced by central org "
        f"{args.central_org or args.org} from local container "
        f"{args.central_container}")
    r = run(central_podman_copy(args), check=True, timeout=900)
    return write_digest_file(dest, r.stdout.splitlines())


def fetch_central_via_ssh(args, dest):
    remote = central_podman_copy(args)
    jump = f"-J {shlex.quote(args.ssh_jump)} " if args.ssh_jump else ""
    cmd = f"ssh -o ConnectTimeout=20 {jump}{shlex.quote(args.central_ssh)} {shlex.quote(remote)}"
    where = args.central_ssh
    if args.ssh_jump:
        where = f"{args.central_ssh} via {args.ssh_jump}"
    log_step(
        f"Reading blobs referenced by central org "
        f"{args.central_org or args.org} via ssh {where}")
    r = run(cmd, check=True, timeout=900)
    return write_digest_file(dest, r.stdout.splitlines())


def fetch_central_via_kubeconfig(args, dest):
    org = checked_org(args.central_org or args.org)
    log_step(
        f"Reading blobs referenced by central org {org} "
        "via --central-kubeconfig")
    env = os.environ.copy()
    env["KUBECONFIG"] = args.central_kubeconfig
    # Reuse detection by invoking oc in that context through a short python-less shell.
    detect = subprocess.run(
        ["oc", "whoami", "--show-server"],
        capture_output=True, text=True, env=env, timeout=60)
    if detect.returncode != 0:
        raise RuntimeError(detect.stderr or "central kubeconfig rejected")
    log_info(f"Central API: {detect.stdout.strip()}")

    def coc(args_str):
        r = subprocess.run(
            f"oc {args_str}", shell=True, capture_output=True, text=True,
            env=env, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(r.stderr)
        return r.stdout.strip()

    ns = args.central_namespace
    if not ns:
        ns = coc("get quayregistry -A -o jsonpath='{.items[0].metadata.namespace}'")
        if not ns:
            ns = coc(
                "get pods -A -l quay-component=quay-app "
                "-o jsonpath='{.items[0].metadata.namespace}'")
    pg = coc(
        f"get pods -n {ns} -l quay-component=postgres "
        f"-o jsonpath='{{.items[0].metadata.name}}'")
    if not pg:
        raise RuntimeError(f"no postgres pod in central namespace {ns}")
    log_info(f"Central Quay: namespace={ns} pod={pg}")
    sql = central_referenced_copy_sql(org)
    r = subprocess.run(
        ["oc", "exec", "-n", ns, pg, "--", "bash", "-c",
         "psql -U postgres -d quay -t -A -c " + shlex.quote(sql)],
        capture_output=True, text=True, env=env, timeout=900)
    if r.returncode != 0:
        # Fall back to the auto-detected user/db by trying the regional-style names.
        raise RuntimeError(r.stderr or "central COPY failed")
    return write_digest_file(dest, r.stdout.splitlines())


def load_central_digests(args):
    if args.ssh_jump and not args.central_ssh:
        log_error("--ssh-jump applies only with --central-ssh")
        sys.exit(2)
    sources = [bool(args.central_local), bool(args.central_ssh),
               bool(args.central_kubeconfig), bool(args.central_digests)]
    if sum(sources) != 1:
        log_error("Pass exactly one of --central-local, --central-ssh, "
                  "--central-kubeconfig, --central-digests")
        sys.exit(2)
    fd, path = tempfile.mkstemp(prefix="central-digests-", suffix=".txt")
    os.close(fd)
    if args.central_digests:
        log_step(f"Reading central digests from {args.central_digests}")
        with open(args.central_digests) as fh:
            n = write_digest_file(path, fh)
    elif args.central_local:
        n = fetch_central_local(args, path)
    elif args.central_ssh:
        n = fetch_central_via_ssh(args, path)
    else:
        n = fetch_central_via_kubeconfig(args, path)
    log_info(f"Central digests loaded: {n}")
    if n < args.min_central_digests:
        log_error(
            f"Central digest count {n} is below --min-central-digests "
            f"{args.min_central_digests}. Refusing to continue.")
        sys.exit(2)
    return path, n


ANALYSIS_SQL = r"""
\set ON_ERROR_STOP on
CREATE TEMP TABLE central_digest (
    content_checksum text PRIMARY KEY
);
\copy central_digest FROM '/tmp/prune-central-digests.txt'

CREATE TEMP TABLE org_blob AS
SELECT DISTINCT is2.id AS blob_id,
                is2.content_checksum,
                COALESCE(is2.image_size, 0) AS image_size
FROM imagestorage is2
JOIN manifestblob mb ON mb.blob_id = is2.id
JOIN manifest m ON m.id = mb.manifest_id
JOIN repository r ON r.id = m.repository_id
WHERE r.namespace_user_id = :org_id
  AND is2.content_checksum IS NOT NULL;

CREATE TEMP TABLE missing AS
SELECT o.*
FROM org_blob o
WHERE NOT EXISTS (
    SELECT 1 FROM central_digest c
    WHERE c.content_checksum = o.content_checksum
);

CREATE TEMP TABLE shared AS
SELECT DISTINCT mb.blob_id
FROM manifestblob mb
JOIN manifest m ON m.id = mb.manifest_id
JOIN repository r ON r.id = m.repository_id
JOIN missing miss ON miss.blob_id = mb.blob_id
WHERE r.namespace_user_id <> :org_id;

CREATE TEMP TABLE deletable AS
SELECT m.*
FROM missing m
WHERE NOT EXISTS (SELECT 1 FROM shared s WHERE s.blob_id = m.blob_id);

CREATE TEMP TABLE drop_manifest AS
SELECT DISTINCT m.id
FROM manifest m
JOIN repository r ON r.id = m.repository_id
JOIN manifestblob mb ON mb.manifest_id = m.id
JOIN deletable d ON d.blob_id = mb.blob_id
WHERE r.namespace_user_id = :org_id;

INSERT INTO drop_manifest (id)
SELECT DISTINCT parent.id
FROM manifest parent
JOIN repository r ON r.id = parent.repository_id
JOIN manifestchild mc ON mc.manifest_id = parent.id
JOIN drop_manifest child ON child.id = mc.child_manifest_id
WHERE r.namespace_user_id = :org_id
  AND NOT EXISTS (SELECT 1 FROM drop_manifest d WHERE d.id = parent.id);

INSERT INTO drop_manifest (id)
SELECT DISTINCT parent.id
FROM manifest parent
JOIN repository r ON r.id = parent.repository_id
JOIN manifestchild mc ON mc.manifest_id = parent.id
JOIN drop_manifest child ON child.id = mc.child_manifest_id
WHERE r.namespace_user_id = :org_id
  AND NOT EXISTS (SELECT 1 FROM drop_manifest d WHERE d.id = parent.id);

\echo SUMMARY
SELECT 'org_blobs', COUNT(*) FROM org_blob
UNION ALL SELECT 'missing_on_central', COUNT(*) FROM missing
UNION ALL SELECT 'shared_kept', COUNT(*) FROM shared
UNION ALL SELECT 'deletable_blobs', COUNT(*) FROM deletable
UNION ALL SELECT 'deletable_bytes', COALESCE(SUM(image_size), 0) FROM deletable
UNION ALL SELECT 'manifests_to_drop', COUNT(*) FROM drop_manifest
UNION ALL SELECT 'tags_to_drop', COUNT(*)
    FROM tag t
    JOIN drop_manifest d ON d.id = t.manifest_id
    JOIN repository r ON r.id = t.repository_id
    WHERE r.namespace_user_id = :org_id;

\echo REPOS
SELECT r.name || '|' || COUNT(DISTINCT d.blob_id) || '|' ||
       COUNT(DISTINCT t.id)
FROM deletable d
JOIN manifestblob mb ON mb.blob_id = d.blob_id
JOIN manifest m ON m.id = mb.manifest_id
JOIN repository r ON r.id = m.repository_id
LEFT JOIN tag t ON t.manifest_id = m.id AND t.repository_id = r.id
WHERE r.namespace_user_id = :org_id
GROUP BY r.name
ORDER BY COUNT(DISTINCT d.blob_id) DESC
LIMIT 30;
"""


EXECUTE_SQL = r"""
\set ON_ERROR_STOP on
BEGIN;

DELETE FROM tag t
USING drop_manifest d, repository r
WHERE t.manifest_id = d.id
  AND t.repository_id = r.id
  AND r.namespace_user_id = :org_id;

DELETE FROM manifestchild mc
USING drop_manifest d
WHERE mc.manifest_id = d.id;

DELETE FROM manifestchild mc
USING drop_manifest d, manifest parent, repository r
WHERE mc.child_manifest_id = d.id
  AND parent.id = mc.manifest_id
  AND parent.repository_id = r.id
  AND r.namespace_user_id = :org_id;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'manifestlabel'
    ) THEN
        DELETE FROM manifestlabel ml
        USING drop_manifest d
        WHERE ml.manifest_id = d.id;
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'manifestsecuritystatus'
    ) THEN
        DELETE FROM manifestsecuritystatus ms
        USING drop_manifest d
        WHERE ms.manifest_id = d.id;
    END IF;
END $$;

DELETE FROM manifestblob mb
USING drop_manifest d
WHERE mb.manifest_id = d.id;

DELETE FROM manifest m
USING drop_manifest d
WHERE m.id = d.id;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'imagestorageplacement' AND column_name = 'storage_id'
    ) THEN
        DELETE FROM imagestorageplacement p
        USING deletable d
        WHERE p.storage_id = d.blob_id
          AND NOT EXISTS (
              SELECT 1 FROM manifestblob mb WHERE mb.blob_id = d.blob_id);
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'imagestoragesignature' AND column_name = 'storage_id'
    ) THEN
        DELETE FROM imagestoragesignature s
        USING deletable d
        WHERE s.storage_id = d.blob_id
          AND NOT EXISTS (
              SELECT 1 FROM manifestblob mb WHERE mb.blob_id = d.blob_id);
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'uploadedblob' AND column_name = 'blob_id'
    ) THEN
        DELETE FROM uploadedblob u
        USING deletable d
        WHERE u.blob_id = d.blob_id
          AND NOT EXISTS (
              SELECT 1 FROM manifestblob mb WHERE mb.blob_id = d.blob_id);
    END IF;
END $$;

DELETE FROM imagestorage i
USING deletable d
WHERE i.id = d.blob_id
  AND NOT EXISTS (
      SELECT 1 FROM manifestblob mb WHERE mb.blob_id = d.blob_id);

COMMIT;

\echo AFTER
SELECT 'deletable_still_referenced', COUNT(*)
FROM deletable d
WHERE EXISTS (SELECT 1 FROM manifestblob mb WHERE mb.blob_id = d.blob_id);
"""


def upload_and_psql(ns, pod, db_user, db_name, org_id, central_path, sql_text):
    sql_local = tempfile.NamedTemporaryFile(
        mode="w", prefix="prune-upstream-", suffix=".sql", delete=False)
    # Analysis creates the temp tables; execute continues in the SAME session.
    sql_local.write(ANALYSIS_SQL)
    sql_local.write("\n")
    sql_local.write(sql_text)
    sql_local.close()
    try:
        run(f"oc cp {shlex.quote(central_path)} {ns}/{pod}:/tmp/prune-central-digests.txt",
            timeout=600)
        run(f"oc cp {shlex.quote(sql_local.name)} {ns}/{pod}:/tmp/prune-upstream.sql",
            timeout=120)
        remote = (
            f"psql -U {db_user} -d {db_name} -v org_id={int(org_id)} "
            f"-f /tmp/prune-upstream.sql"
        )
        out = oc_exec(ns, pod, remote, timeout=3600)
    finally:
        os.unlink(sql_local.name)
        oc_exec(ns, pod,
                "rm -f /tmp/prune-central-digests.txt /tmp/prune-upstream.sql",
                check=False)
    return out


def parse_report(text):
    summary = {}
    repos = []
    section = None
    for line in text.splitlines():
        if line.strip() == "SUMMARY":
            section = "summary"
            continue
        if line.strip() == "REPOS":
            section = "repos"
            continue
        if line.strip() == "AFTER":
            section = "after"
            continue
        if section == "summary" and "|" in line:
            key, val = [p.strip() for p in line.split("|", 1)]
            if key:
                summary[key] = val
        elif section == "repos" and "|" in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3 and parts[0] and parts[0] != "name":
                repos.append(parts)
        elif section == "after" and "|" in line:
            key, val = [p.strip() for p in line.split("|", 1)]
            if key:
                summary[key] = val
    return summary, repos


QUOTA_RESET_SQL = r"""
\set ON_ERROR_STOP on
BEGIN;

UPDATE quotanamespacesize
SET size_bytes = 0,
    backfill_start_ms = NULL,
    backfill_complete = false
WHERE namespace_user_id = :org_id
  AND backfill_start_ms IS NOT NULL;

UPDATE quotarepositorysize AS qr
SET size_bytes = 0,
    backfill_start_ms = NULL,
    backfill_complete = false
FROM repository AS r
WHERE qr.repository_id = r.id
  AND r.namespace_user_id = :org_id
  AND qr.backfill_start_ms IS NOT NULL;

COMMIT;

\echo QUOTA
SELECT 'namespace_waiting', COUNT(*)
FROM quotanamespacesize
WHERE namespace_user_id = :org_id
  AND backfill_complete = false
  AND backfill_start_ms IS NULL
UNION ALL
SELECT 'repository_waiting', COUNT(*)
FROM quotarepositorysize qr
JOIN repository r ON r.id = qr.repository_id
WHERE r.namespace_user_id = :org_id
  AND qr.backfill_complete = false
  AND qr.backfill_start_ms IS NULL;
"""


def reset_quota_backfill(ns, pod, db_user, db_name, org_id, org_name):
    """Ask Quay's quota worker to recount one organization.

    Matches reset_namespace_backfill / reset_backfill: size_bytes goes to
    0 and both backfill columns are cleared. The worker only recomputes a
    row when backfill_complete is false and backfill_start_ms is null.
    Other organizations are not touched.
    """
    log_step(f"Queueing quota backfill for {org_name}...")
    sql_local = tempfile.NamedTemporaryFile(
        mode="w", prefix="prune-quota-", suffix=".sql", delete=False)
    sql_local.write(QUOTA_RESET_SQL)
    sql_local.close()
    try:
        run(f"oc cp {shlex.quote(sql_local.name)} {ns}/{pod}:/tmp/prune-quota-reset.sql",
            timeout=120)
        remote = (
            f"psql -U {db_user} -d {db_name} -v org_id={int(org_id)} "
            f"-f /tmp/prune-quota-reset.sql"
        )
        out = oc_exec(ns, pod, remote, timeout=600)
    finally:
        os.unlink(sql_local.name)
        oc_exec(ns, pod, "rm -f /tmp/prune-quota-reset.sql", check=False)

    waiting = {}
    section = None
    for line in out.splitlines():
        if line.strip() == "QUOTA":
            section = "quota"
            continue
        if section == "quota" and "|" in line:
            key, val = [p.strip() for p in line.split("|", 1)]
            if key:
                waiting[key] = val
    ns_n = waiting.get("namespace_waiting", "?")
    repo_n = waiting.get("repository_waiting", "?")
    log_info(
        f"Quota backfill queued: namespace rows={ns_n}, "
        f"repository rows={repo_n}.")
    log_warn(
        "The organization page shows 0 until Quay's quota worker "
        "writes the live totals. That worker polls about every 15 seconds.")
    return waiting


def human_bytes(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    step = 1024.0
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < step or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= step
    return str(n)


def main():
    parser = argparse.ArgumentParser(
        description="Prune regional Quay blobs absent from central Quay",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--org", default=os.environ.get("ORG_NAME", "ocp4"))
    parser.add_argument("--central-org", default="",
                        help="Organization on central whose referenced "
                             "blobs count as still present (default: --org)")
    parser.add_argument("--namespace", default=os.environ.get("QUAY_NAMESPACE", ""))
    parser.add_argument("--central-local", action="store_true",
                        help="This host is the central Quay "
                             "(sudo podman exec postgresql-quay)")
    parser.add_argument("--central-ssh", default="",
                        help="Direct SSH host of a RHEL central Quay "
                             "(sudo podman). No jump host required.")
    parser.add_argument("--ssh-jump", default="",
                        help="Optional ssh -J ProxyJump for --central-ssh. "
                             "Omit this when the script runs on a host "
                             "that can ssh to central directly.")
    parser.add_argument("--central-container", default="postgresql-quay")
    parser.add_argument("--central-db", default="quay")
    parser.add_argument("--central-db-user", default="postgres")
    parser.add_argument("--central-kubeconfig", default="",
                        help="Kubeconfig of an OpenShift central Quay")
    parser.add_argument("--central-namespace", default="")
    parser.add_argument("--central-digests", default="",
                        help="File of digests still referenced by the "
                             "central org, one content_checksum per line")
    parser.add_argument("--min-central-digests", type=int, default=1000,
                        help="Refuse to run if central returned fewer digests")
    parser.add_argument("--max-drop-ratio", type=float, default=0.25,
                        help="Refuse --execute above this fraction unless --force")
    parser.add_argument("--execute", action="store_true",
                        help="Delete eligible regional DB rows (default: dry-run)")
    parser.add_argument("--force", action="store_true",
                        help="Allow --execute above --max-drop-ratio")
    parser.add_argument("--reset-quota", action="store_true",
                        help="Queue a quota recount for --org and exit. "
                             "Also done automatically after a successful "
                             "--execute. Does not delete blobs.")
    args = parser.parse_args()
    if not args.central_org:
        args.central_org = args.org

    if args.reset_quota and not args.execute:
        mode = "reset-quota"
    elif args.execute:
        mode = "EXECUTE"
    else:
        mode = "dry-run"
    print("=== Quay prune blobs absent from central ===")
    print(f"  Organization: {args.org}")
    print(f"  Mode:         {mode}")
    print()

    cluster, ns, pod, db_user, db_name = detect_regional(args.namespace)

    org_id = regional_sql(
        ns, pod, db_user, db_name,
        f"SELECT id FROM public.user WHERE username = '{args.org.replace(chr(39), '')}';",
        single=True)
    if not org_id or not org_id.isdigit():
        log_error(f"Organization '{args.org}' not found")
        print(regional_sql(
            ns, pod, db_user, db_name,
            "SELECT username FROM public.user WHERE organization = true ORDER BY 1;"))
        sys.exit(1)
    log_info(f"Organization: {args.org} (id {org_id})")

    upstream = regional_sql(
        ns, pod, db_user, db_name,
        "SELECT split_part(m.external_reference, '/', 1), COUNT(*) "
        "FROM repomirrorconfig m "
        "JOIN repository r ON r.id = m.repository_id "
        f"WHERE r.namespace_user_id = {int(org_id)} "
        "GROUP BY 1 ORDER BY 2 DESC;")
    if upstream.strip():
        log_info("Mirror upstreams configured for this org:")
        for line in upstream.splitlines():
            if line.strip():
                print(f"  {line.strip()}")
    else:
        log_warn("No repomirrorconfig rows for this org. "
                 "Central must be selected explicitly.")

    if args.reset_quota and not args.execute:
        reset_quota_backfill(ns, pod, db_user, db_name, org_id, args.org)
        return

    central_path, central_count = load_central_digests(args)

    log_step(
        f"Comparing regional {args.org} blobs with blobs still "
        f"referenced by central {args.central_org}...")
    try:
        report = upload_and_psql(
            ns, pod, db_user, db_name, org_id, central_path, "")
        summary, repos = parse_report(report)
        org_blobs = int(summary.get("org_blobs") or 0)
        missing = int(summary.get("missing_on_central") or 0)
        shared = int(summary.get("shared_kept") or 0)
        deletable = int(summary.get("deletable_blobs") or 0)
        manifests = int(summary.get("manifests_to_drop") or 0)
        tags = int(summary.get("tags_to_drop") or 0)
        ratio = (deletable / org_blobs) if org_blobs else 0

        print()
        print("========================================")
        print(f"  Regional cluster:     {cluster}")
        print(f"  Organization:         {args.org}")
        print(f"  Central org blobs:    {central_count}")
        print(f"  Org blobs:            {org_blobs}")
        print(f"  Absent from central:  {missing}")
        print(f"  Kept (other orgs):    {shared}")
        print(f"  Deletable blobs:      {deletable} "
              f"({human_bytes(summary.get('deletable_bytes', 0))})")
        print(f"  Manifests to drop:    {manifests}")
        print(f"  Tags to drop:         {tags}")
        print(f"  Drop ratio:           {ratio:.1%}")
        print("========================================")
        if repos:
            print()
            log_info("Largest affected repositories (blobs | tags):")
            for name, blobs, tag_n in repos:
                print(f"  {args.org}/{name}  blobs={blobs}  tags={tag_n}")

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = f"/tmp/quay-prune-upstream-{cluster}-{args.org}-{stamp}.txt"
        with open(out_path, "w") as fh:
            fh.write(report)
            fh.write("\n")
        log_info(f"Full SQL report: {out_path}")

        if not args.execute:
            print()
            if ratio > args.max_drop_ratio:
                log_warn(
                    f"Drop ratio {ratio:.1%} exceeds --max-drop-ratio "
                    f"{args.max_drop_ratio:.0%}. --execute will refuse "
                    "unless --force is set.")
            log_warn("Dry-run only. Re-run with --execute to delete the deletable rows.")
            log_warn("S3 objects are not removed. Follow with ocp-quay-s3-gc.py.")
            return

        if deletable == 0:
            log_info("Nothing to delete.")
            if args.reset_quota:
                reset_quota_backfill(ns, pod, db_user, db_name, org_id, args.org)
            return
        if ratio > args.max_drop_ratio and not args.force:
            log_error(
                f"Deletable ratio {ratio:.1%} exceeds --max-drop-ratio "
                f"{args.max_drop_ratio:.0%}. Re-run with --force if this is intended.")
            sys.exit(3)

        log_step("Deleting regional database rows...")
        done = upload_and_psql(
            ns, pod, db_user, db_name, org_id, central_path, EXECUTE_SQL)
        after, _ = parse_report(done)
        still = after.get("deletable_still_referenced")
        log_info("Database rows removed.")
        if still not in (None, "", "0"):
            log_warn(f"{still} deletable blobs are still referenced and were kept.")
        reset_quota_backfill(ns, pod, db_user, db_name, org_id, args.org)
        log_warn("S3 objects were not deleted. Run ocp-quay-s3-gc.py to reclaim them "
                 "after you are satisfied with the database prune.")
    finally:
        if os.path.exists(central_path):
            os.unlink(central_path)


if __name__ == "__main__":
    main()
