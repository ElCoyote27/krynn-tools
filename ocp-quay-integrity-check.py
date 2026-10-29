#!/usr/bin/env python3
"""
Check Quay blob integrity on OCP-based Quay (operator deployment).

Verifies that blobs referenced in the Quay database actually exist in
S3/ODF storage.  Reports (and optionally repairs) missing blobs that
cause image pull failures.

Kubeconfig: uses $KUBECONFIG or ~/.kube/config (same as oc).
Quay namespace, database pod, user, and S3 config are all auto-detected.

Self-contained: this script only needs oc, skopeo (for --fix-missing),
and python3 (with PyYAML — present on RHEL/CoreOS).  Copy it to any
host with kubeconfig access and network connectivity to the Quay
endpoints.

Jumphost workflow (recommended for --fix-missing):
  The scan phase uses 'oc exec' for DB queries (fast, works from anywhere).
  The --fix-missing phase uses 'skopeo copy' for blob transfers, which
  benefits from network proximity to the Quay endpoints.

  For best performance, copy this script to a jumphost in the same DC:
    scp ocp-check-quay-integrity.py installer0:
    ssh installer0
    export KUBECONFIG=/path/to/kubeconfig
    python3 ocp-check-quay-integrity.py --namespace quay --fix-missing
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

# ─── Colors ──────────────────────────────────────────────────────────
RED    = "\033[0;31m"
GREEN  = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN   = "\033[0;36m"
NC     = "\033[0m"

def log_info(msg):  print(f"{GREEN}[INFO]{NC} {msg}")
def log_warn(msg):  print(f"{YELLOW}[WARN]{NC} {msg}")
def log_error(msg): print(f"{RED}[ERROR]{NC} {msg}")
def log_step(msg):  print(f"{CYAN}[STEP]{NC} {msg}")


# ─── Shell helpers ───────────────────────────────────────────────────
def run(cmd, check=True, capture=True):
    """Run a shell command, return stdout string."""
    r = subprocess.run(cmd, shell=True, capture_output=capture,
                       text=True, timeout=300)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed ({r.returncode}): {cmd}\n{r.stderr}")
    return r.stdout.strip() if capture else ""


def oc(args, check=True):
    """Run an oc command."""
    return run(f"oc {args}", check=check)


def oc_exec(ns, pod, cmd, check=True, timeout=600):
    """oc exec into a pod."""
    r = subprocess.run(
        ["oc", "exec", "-n", ns, pod, "--", "bash", "-c", cmd],
        capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"oc exec failed: {cmd}\n{r.stderr}")
    return r.stdout.strip()


def run_sql(ns, pg_pod, db_user, db_name, sql, single_value=False):
    """Run SQL via oc exec into the postgres pod."""
    flags = "-t -A" if single_value else "-t"
    escaped = sql.replace("'", "'\\''")
    out = oc_exec(ns, pg_pod, f"psql -U {db_user} -d {db_name} {flags} -c '{escaped}'")
    if single_value:
        return out.strip().strip(" \n\r")
    return out


# ─── S3 config extraction ───────────────────────────────────────────
def extract_s3_config(config_yaml):
    """Parse Quay config.yaml and return S3 storage parameters."""
    try:
        import yaml
        cfg = yaml.safe_load(config_yaml)
    except ImportError:
        log_warn("PyYAML not available, falling back to regex parser")
        return _extract_s3_regex(config_yaml)

    dist = cfg.get("DISTRIBUTED_STORAGE_CONFIG", {})
    if not dist:
        raise RuntimeError("DISTRIBUTED_STORAGE_CONFIG not found in Quay config")

    name = list(dist.keys())[0]
    entry = dist[name]
    storage_type = entry[0] if isinstance(entry, list) else "unknown"
    params = entry[1] if isinstance(entry, list) and len(entry) > 1 else entry

    if not isinstance(params, dict):
        raise RuntimeError(f"Unexpected storage config format for '{name}'")

    ak = params.get("access_key", params.get("s3_access_key", ""))
    sk = params.get("secret_key", params.get("s3_secret_key", ""))
    bucket = params.get("bucket_name", params.get("s3_bucket", ""))
    sp = params.get("storage_path", "/datastorage/registry").lstrip("/")
    host = params.get("hostname", params.get("host", ""))
    port = str(params.get("port", ""))
    secure = str(params.get("is_secure", "true")).lower() in ("true", "yes", "1")

    endpoint = f"{'https' if secure else 'http'}://{host}"
    if port and port not in ("443", "80"):
        endpoint += f":{port}"

    return {
        "access_key": ak, "secret_key": sk, "bucket": bucket,
        "path": sp, "endpoint": endpoint, "type": storage_type,
    }


def _extract_s3_regex(config_yaml):
    """Fallback regex parser (no PyYAML)."""
    import re
    params = {}
    in_storage = in_s3 = False
    for line in config_yaml.splitlines():
        s = line.strip().lstrip("- ")
        if s.startswith("DISTRIBUTED_STORAGE_CONFIG"):
            in_storage = True; continue
        if in_storage and line and not line[0].isspace() and ":" in s:
            in_storage = in_s3 = False; continue
        if in_storage and any(t in s for t in ("S3Storage", "RadosGWStorage", "RHOCSStorage")):
            in_s3 = True; continue
        if in_s3:
            m = re.match(
                r"\s*-?\s*(access_key|s3_access_key|secret_key|s3_secret_key|"
                r"s3_bucket|bucket_name|storage_path|host|hostname|port|is_secure)"
                r":\s*(.*)", line)
            if m:
                params[m.group(1)] = m.group(2).strip().strip("\"' ")

    host = params.get("host", params.get("hostname", ""))
    port = params.get("port", "")
    secure = params.get("is_secure", "true").lower() in ("true", "yes", "1")
    endpoint = f"{'https' if secure else 'http'}://{host}"
    if port and port not in ("443", "80"):
        endpoint += f":{port}"

    return {
        "access_key": params.get("access_key", params.get("s3_access_key", "")),
        "secret_key": params.get("secret_key", params.get("s3_secret_key", "")),
        "bucket": params.get("s3_bucket", params.get("bucket_name", "")),
        "path": params.get("storage_path", "/datastorage/registry").lstrip("/"),
        "endpoint": endpoint,
    }


# ─── The python script that runs inside the quay pod ────────────────
CHECKER_SCRIPT = r'''
import boto3, botocore, sys, json, urllib3
urllib3.disable_warnings()
cfg = json.load(open('/tmp/_integrity_creds.json'))
s3 = boto3.client('s3', endpoint_url=cfg['endpoint'],
    aws_access_key_id=cfg['access_key'],
    aws_secret_access_key=cfg['secret_key'], verify=False)
BUCKET, PREFIX = cfg['bucket'], cfg['path']
BATCH   = cfg.get('batch_size', 500)
VERBOSE = cfg.get('verbose', False)
missing, checked, ok_count, errors = [], 0, 0, 0
for line in sys.stdin:
    cs = line.strip()
    if not cs or not cs.startswith('sha256:'): continue
    digest = cs[7:]
    key = f"{PREFIX}/sha256/{digest[:2]}/{digest}"
    try:
        s3.head_object(Bucket=BUCKET, Key=key); ok_count += 1
    except botocore.exceptions.ClientError as e:
        code = e.response['Error']['Code']
        if code in ('404', 'NoSuchKey'): missing.append(cs)
        else:
            errors += 1
            if VERBOSE: print(f"S3_ERROR {cs} {code}", file=sys.stderr, flush=True)
    except Exception as e:
        errors += 1
        if VERBOSE: print(f"S3_ERROR {cs} {e}", file=sys.stderr, flush=True)
    checked += 1
    if checked % BATCH == 0: print(f"PROGRESS {checked} {len(missing)} {errors}", flush=True)
print(f"PROGRESS {checked} {len(missing)} {errors}", flush=True)
print("RESULTS_START", flush=True)
print(json.dumps({"checked": checked, "ok": ok_count, "missing": len(missing),
    "errors": errors, "missing_digests": missing}), flush=True)
print("RESULTS_END", flush=True)
'''


# ─── Main ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Quay Blob Integrity Checker for OCP-based Quay deployments",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              # Scan regional quay
              export KUBECONFIG=/path/to/kubeconfig
              python3 ocp-check-quay-integrity.py --org ocp4

              # Full auto-repair
              python3 ocp-check-quay-integrity.py --org ocp4 --fix-missing

              # Just purge stale refs
              python3 ocp-check-quay-integrity.py --org ocp4 --fix

            Jumphost workflow (recommended for --fix-missing):
              scp ocp-check-quay-integrity.py installer0:
              ssh installer0
              export KUBECONFIG=/path/to/kubeconfig
              python3 ocp-check-quay-integrity.py --namespace quay --fix-missing
        """),
    )
    parser.add_argument("--org", default=os.environ.get("ORG_NAME", ""),
                        help="Check only this organization (default: all)")
    parser.add_argument("--namespace", default=os.environ.get("QUAY_NAMESPACE", ""),
                        help="Quay namespace (auto-detected)")
    parser.add_argument("--list-orgs", action="store_true",
                        help="List organizations and exit")
    parser.add_argument("--fix", action="store_true",
                        help="Purge stale DB refs for missing blobs")
    parser.add_argument("--fix-missing", action="store_true",
                        help="Full repair: flip Mirror→Normal, purge, re-sync, verify")
    parser.add_argument("--authfile", default="",
                        help="Auth file for skopeo (auto-extracted if not given)")
    parser.add_argument("--verbose", action="store_true",
                        help="Show per-blob S3 errors")
    parser.add_argument("--batch-size", type=int,
                        default=int(os.environ.get("BATCH_SIZE", "500")),
                        help="Blobs per progress batch (default: 500)")
    args = parser.parse_args()

    if args.fix_missing:
        args.fix = True

    quay_ns = args.namespace
    org_name = args.org

    print()
    print("=== Quay Blob Integrity Check (OCP) ===")
    print()

    # ─── Verify cluster access ──────────────────────────────────────
    try:
        oc("cluster-info")
    except Exception:
        log_error("Cannot connect to cluster. Check $KUBECONFIG or ~/.kube/config.")
        sys.exit(1)
    log_info("Cluster access: OK")

    # ─── Auto-detect namespace ──────────────────────────────────────
    if not quay_ns:
        log_info("Auto-detecting Quay namespace...")
        quay_ns = oc("get quayregistry -A -o jsonpath='{.items[0].metadata.namespace}'",
                     check=False)
        if not quay_ns:
            quay_ns = oc("get pods -A -l quay-component=quay-app "
                         "-o jsonpath='{.items[0].metadata.namespace}'", check=False)
        if not quay_ns:
            log_error("Could not auto-detect Quay namespace. Use --namespace.")
            sys.exit(1)
    log_info(f"Namespace: {quay_ns}")

    # ─── Detect PostgreSQL pod ──────────────────────────────────────
    pg_pod = oc(f"get pods -n {quay_ns} -l quay-component=postgres "
                f"-o jsonpath='{{.items[0].metadata.name}}'", check=False)
    if not pg_pod:
        out = oc(f"get pods -n {quay_ns} -o name", check=False)
        for line in out.splitlines():
            if "database" in line.lower() and "clair" not in line.lower():
                pg_pod = line.replace("pod/", ""); break
    if not pg_pod:
        log_error(f"Could not find PostgreSQL pod in namespace {quay_ns}")
        sys.exit(1)
    log_info(f"PostgreSQL pod: {pg_pod}")

    # ─── Detect Quay app pod ────────────────────────────────────────
    quay_pod = oc(f"get pods -n {quay_ns} -l quay-component=quay-app "
                  f"-o jsonpath='{{.items[0].metadata.name}}'", check=False)
    if not quay_pod:
        out = oc(f"get pods -n {quay_ns} -o name", check=False)
        for line in out.splitlines():
            if "quay" in line.lower() and "app" in line.lower():
                quay_pod = line.replace("pod/", ""); break
    if not quay_pod:
        log_error("Could not find Quay app pod (needed for S3 access)")
        sys.exit(1)
    log_info(f"Quay app pod: {quay_pod}")

    # ─── Detect database name and user ──────────────────────────────
    db_user_out = oc_exec(quay_ns, pg_pod,
        "psql -U postgres -t -c \"SELECT usename FROM pg_user WHERE usename != 'postgres';\"",
        check=False)
    db_user = ""
    for line in db_user_out.splitlines():
        if "quay" in line.lower():
            db_user = line.strip(); break
    if not db_user:
        db_user = "postgres"

    db_name_out = oc_exec(quay_ns, pg_pod,
        f"psql -U {db_user} -t -c \"SELECT datname FROM pg_database "
        f"WHERE datname NOT IN ('postgres','template0','template1');\"",
        check=False)
    db_name = ""
    for line in db_name_out.splitlines():
        if "quay" in line.lower():
            db_name = line.strip(); break
    if not db_name:
        db_name = "quay"
    log_info(f"Database: {db_name} (user: {db_user})")

    # ─── Helper shortcut ────────────────────────────────────────────
    def sql(query, single=False):
        return run_sql(quay_ns, pg_pod, db_user, db_name, query, single_value=single)

    # ─── List orgs ──────────────────────────────────────────────────
    if args.list_orgs:
        log_info(f"Organizations in {db_name}:")
        print(sql("SELECT username FROM public.user WHERE organization = true ORDER BY username;"))
        sys.exit(0)

    # ─── Extract S3 config ──────────────────────────────────────────
    log_step("Extracting S3 storage configuration...")

    config_secret = ""
    secrets_out = oc(f"get secrets -n {quay_ns} -o name", check=False)
    for line in secrets_out.splitlines():
        name = line.replace("secret/", "").strip()
        if "quay" in name.lower() and "config-secret" in name.lower() \
                and "clair" not in name.lower() and "postgres" not in name.lower():
            config_secret = name; break

    if not config_secret:
        config_secret = oc(f"get quayregistry -n {quay_ns} "
                           f"-o jsonpath='{{.items[0].spec.configBundleSecret}}'",
                           check=False)
    if not config_secret:
        for line in secrets_out.splitlines():
            name = line.replace("secret/", "").strip()
            if "config-bundle" in name.lower():
                config_secret = name; break

    if not config_secret:
        log_error("Could not find Quay config secret")
        sys.exit(1)

    config_b64 = oc(f"get secret {config_secret} -n {quay_ns} "
                    f"-o jsonpath='{{.data.config\\.yaml}}'", check=False)
    if not config_b64:
        log_error("Could not read Quay config from secret")
        sys.exit(1)

    import base64
    config_yaml = base64.b64decode(config_b64).decode("utf-8")

    s3 = extract_s3_config(config_yaml)

    if not s3["access_key"] or not s3["secret_key"]:
        log_error("S3 access_key or secret_key is empty — check Quay config")
        sys.exit(1)

    log_info(f"S3 Bucket:   {s3['bucket']}")
    log_info(f"S3 Path:     {s3['path']}")
    log_info(f"S3 Endpoint: {s3['endpoint']}")

    # ─── Build org filter ───────────────────────────────────────────
    org_id = None
    if org_name:
        org_id = sql(f"SELECT id FROM public.user WHERE username = '{org_name}';", single=True)
        if not org_id:
            log_error(f"Organization '{org_name}' not found")
            print(sql("SELECT username FROM public.user WHERE organization = true;"))
            sys.exit(1)
        log_info(f"Organization: {org_name} (ID: {org_id})")
        org_filter = (f"JOIN manifestblob mb ON mb.blob_id = is2.id "
                      f"JOIN manifest m ON mb.manifest_id = m.id "
                      f"JOIN repository r ON m.repository_id = r.id "
                      f"WHERE r.namespace_user_id = {org_id} AND")
    else:
        log_info("Organization: ALL")
        org_filter = "WHERE"

    # ─── Count blobs ────────────────────────────────────────────────
    log_step("Counting blobs to verify...")
    blob_count = int(sql(
        f"SELECT count(DISTINCT is2.id) FROM imagestorage is2 "
        f"{org_filter} is2.content_checksum IS NOT NULL;", single=True) or "0")

    print()
    print("========================================")
    print(f"  Blobs to verify: {blob_count}")
    print("========================================")
    print()

    if blob_count == 0:
        log_info("No blobs to check.")
        sys.exit(0)

    # ─── Dump digests ───────────────────────────────────────────────
    log_step("Extracting blob digests from database...")
    digest_output = sql(
        f"SELECT DISTINCT is2.content_checksum FROM imagestorage is2 "
        f"{org_filter} is2.content_checksum IS NOT NULL;")

    digest_lines = [l.strip() for l in digest_output.splitlines()
                    if l.strip().startswith("sha256:")]
    actual_count = len(digest_lines)
    log_info(f"Extracted {actual_count} unique digests")

    # Write digests to temp file
    digest_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                              prefix="quay_digests_", delete=False)
    digest_file.write("\n".join(digest_lines) + "\n")
    digest_file.close()

    # ─── Write checker script and creds to temp files ───────────────
    log_step("Running integrity check inside Quay pod (S3 HEAD per blob)...")

    check_file = tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                             prefix="quay_check_", delete=False)
    check_file.write(CHECKER_SCRIPT)
    check_file.close()

    creds_data = {
        "access_key": s3["access_key"],
        "secret_key": s3["secret_key"],
        "bucket": s3["bucket"],
        "path": s3["path"],
        "endpoint": s3["endpoint"],
        "batch_size": args.batch_size,
        "verbose": args.verbose,
    }
    creds_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                             prefix="quay_creds_", delete=False)
    json.dump(creds_data, creds_file)
    creds_file.close()

    # ─── Upload scripts and creds ──────────────────────────────────
    try:
        run(f"oc cp {check_file.name} {quay_ns}/{quay_pod}:/tmp/_integrity_check.py")
        run(f"oc cp {creds_file.name} {quay_ns}/{quay_pod}:/tmp/_integrity_creds.json")
    finally:
        os.unlink(check_file.name)
        os.unlink(creds_file.name)

    # ─── Run in chunks to avoid oc exec timeouts on large sets ──────
    CHUNK_SIZE = 10000
    all_missing = []
    total_checked = total_ok = total_errors = 0

    for chunk_start in range(0, actual_count, CHUNK_SIZE):
        chunk_end = min(chunk_start + CHUNK_SIZE, actual_count)
        chunk_lines = digest_lines[chunk_start:chunk_end]
        chunk_num = chunk_start // CHUNK_SIZE + 1
        total_chunks = (actual_count + CHUNK_SIZE - 1) // CHUNK_SIZE

        if total_chunks > 1:
            print(f"  Chunk {chunk_num}/{total_chunks}: ", end="", flush=True)

        # Write chunk to temp file and upload
        chunk_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                                 prefix="quay_chunk_", delete=False)
        chunk_file.write("\n".join(chunk_lines) + "\n")
        chunk_file.close()
        try:
            run(f"oc cp {chunk_file.name} {quay_ns}/{quay_pod}:/tmp/_integrity_digests.txt")
        finally:
            os.unlink(chunk_file.name)

        raw_output = oc_exec(quay_ns, quay_pod,
            "cat /tmp/_integrity_digests.txt | python3 /tmp/_integrity_check.py",
            check=False, timeout=3600)

        # Parse progress — show only the last PROGRESS line per chunk
        last_progress = ""
        for line in raw_output.splitlines():
            if line.startswith("PROGRESS "):
                parts = line.split()
                if len(parts) >= 4:
                    done = chunk_start + int(parts[1])
                    miss = len(all_missing) + int(parts[2])
                    errs = total_errors + int(parts[3])
                    last_progress = (f"Checked: {done} / {actual_count} | "
                                     f"Missing: {miss} | Errors: {errs}")
        if last_progress:
            print(last_progress, flush=True)

        # Parse results
        chunk_results = None
        capture = False
        for line in raw_output.splitlines():
            if line.strip() == "RESULTS_START":
                capture = True; continue
            if line.strip() == "RESULTS_END":
                capture = False; continue
            if capture:
                try:
                    chunk_results = json.loads(line)
                except json.JSONDecodeError:
                    pass

        if not chunk_results:
            log_error(f"Failed to get results for chunk {chunk_num} "
                      f"(digests {chunk_start+1}-{chunk_end})")
            log_warn("oc exec may have timed out — retrying this chunk...")
            # One retry
            raw_output = oc_exec(quay_ns, quay_pod,
                "cat /tmp/_integrity_digests.txt | python3 /tmp/_integrity_check.py",
                check=False, timeout=3600)
            capture = False
            for line in raw_output.splitlines():
                if line.strip() == "RESULTS_START":
                    capture = True; continue
                if line.strip() == "RESULTS_END":
                    capture = False; continue
                if capture:
                    try:
                        chunk_results = json.loads(line)
                    except json.JSONDecodeError:
                        pass
            if not chunk_results:
                log_error(f"Chunk {chunk_num} failed after retry — aborting")
                print(raw_output[-2000:] if len(raw_output) > 2000 else raw_output)
                sys.exit(1)

        total_checked += chunk_results["checked"]
        total_ok      += chunk_results["ok"]
        total_errors  += chunk_results["errors"]
        all_missing.extend(chunk_results.get("missing_digests", []))

    os.unlink(digest_file.name)
    print()

    total_missing = len(all_missing)
    missing_digests = all_missing
    results_json = {
        "checked": total_checked, "ok": total_ok,
        "missing": total_missing, "errors": total_errors,
        "missing_digests": missing_digests,
    }

    total_checked = results_json["checked"]
    total_ok      = results_json["ok"]
    total_missing = results_json["missing"]
    total_errors  = results_json["errors"]
    missing_digests = results_json.get("missing_digests", [])

    # ─── Display results ────────────────────────────────────────────
    print()
    print("========================================")
    print("  INTEGRITY CHECK RESULTS")
    print("========================================")
    print(f"  Checked:  {CYAN}{total_checked}{NC}")
    print(f"  OK:       {GREEN}{total_ok}{NC}")
    print(f"  Missing:  {RED}{total_missing}{NC}")
    print(f"  Errors:   {YELLOW}{total_errors}{NC}")
    print("========================================")

    if total_missing == 0 and total_errors == 0:
        print()
        log_info("All blobs verified successfully ✓")
        sys.exit(0)

    # ─── List missing blobs ─────────────────────────────────────────
    if total_missing > 0:
        print()
        if total_missing <= 50:
            log_warn(f"Missing blobs ({total_missing}):")
            for d in missing_digests:
                print(f"  {d}")
            print()
            log_info("Affected images:")
            for digest in missing_digests:
                repos = sql(
                    f"SELECT DISTINCT r.name FROM repository r "
                    f"JOIN manifest m ON m.repository_id = r.id "
                    f"JOIN manifestblob mb ON mb.manifest_id = m.id "
                    f"JOIN imagestorage is2 ON mb.blob_id = is2.id "
                    f"WHERE is2.content_checksum = '{digest}';")
                for repo in repos.splitlines():
                    repo = repo.strip()
                    if repo:
                        print(f"  {YELLOW}{digest}{NC} → {repo}")
        else:
            log_warn(f"Missing blobs: {total_missing} (too many to list individually)")
            log_info("First 10:")
            for d in missing_digests[:10]:
                print(f"  {d}")
            log_info("Full list saved in results JSON")

    # ─── Fix modes ──────────────────────────────────────────────────
    if args.fix_missing and total_missing > 0:
        _fix_missing(args, quay_ns, quay_pod, pg_pod, db_user, db_name,
                     results_json, sql)

    elif args.fix and total_missing > 0:
        _fix_purge(quay_ns, pg_pod, db_user, db_name, missing_digests, sql)

    elif total_missing > 0:
        print()
        log_warn("Use --fix to purge stale DB references")
        log_warn("Use --fix-missing for full repair (flip repo, re-sync, verify)")

    # ─── Save results ───────────────────────────────────────────────
    from datetime import datetime
    save_file = f"/tmp/quay-integrity-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    with open(save_file, "w") as f:
        json.dump(results_json, f, indent=2)
    log_info(f"Full results saved to: {save_file}")


# ─── Fix: purge stale refs only ──────────────────────────────────────
def _fix_purge(quay_ns, pg_pod, db_user, db_name, missing_digests, sql):
    print()
    total = len(missing_digests)
    log_step(f"Purging stale DB references for {total} missing blobs...")
    fixed = 0
    failed = 0
    for i, digest in enumerate(missing_digests, 1):
        try:
            blob_id = sql(f"SELECT id FROM imagestorage WHERE content_checksum = '{digest}';",
                          single=True)
            if blob_id:
                sql(f"BEGIN; "
                    f"DELETE FROM manifestblob WHERE blob_id = {blob_id}; "
                    f"DELETE FROM imagestorageplacement WHERE storage_id = {blob_id}; "
                    f"DELETE FROM uploadedblob WHERE blob_id = {blob_id}; "
                    f"DELETE FROM imagestoragesignature WHERE storage_id = {blob_id}; "
                    f"DELETE FROM imagestorage WHERE id = {blob_id}; "
                    f"COMMIT;")
                fixed += 1
            if i % 100 == 0:
                print(f"\r  Progress: {i}/{total} (purged: {fixed}, failed: {failed})",
                      end="", flush=True)
        except Exception as e:
            failed += 1
            if failed <= 10:
                log_warn(f"Failed to purge: {digest} — {e}")
            elif failed == 11:
                log_warn("(suppressing further failure messages)")
    print()
    log_info(f"Purged {fixed} / {total} stale blob references ({failed} failures)")
    if failed > 0:
        log_warn("Re-run to retry failures (transient oc exec timeouts)")


# ─── Fix: full repair ───────────────────────────────────────────────
def _fix_missing(args, quay_ns, quay_pod, pg_pod, db_user, db_name,
                 results_json, sql):
    missing_digests = results_json.get("missing_digests", [])
    total_missing = len(missing_digests)

    print()
    print("========================================")
    print("  FIX-MISSING: Full Blob Repair")
    print("========================================")
    print()

    # Ensure auth file
    authfile = args.authfile
    authfile_tmp = False
    if not authfile:
        log_info("Extracting pull-secret from cluster...")
        ps = oc("get secret -n openshift-config pull-secret "
                "-o jsonpath='{.data.\\.dockerconfigjson}'", check=False)
        if ps:
            import base64
            ps_decoded = base64.b64decode(ps).decode("utf-8")
            f = tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                            prefix="quay_ps_", delete=False)
            f.write(ps_decoded)
            f.close()
            authfile = f.name
            authfile_tmp = True
        else:
            log_error("Could not extract pull-secret. Use --authfile.")
            sys.exit(1)

    # Get Quay route
    quay_host = oc(f"get route -n {quay_ns} -l quay-component=quay-app "
                   f"-o jsonpath='{{.items[0].spec.host}}'", check=False)
    if not quay_host:
        quay_host = oc(f"get route -n {quay_ns} "
                       f"-o jsonpath='{{.items[0].spec.host}}'", check=False)
    log_info(f"Quay registry: {quay_host}")

    # Map missing blobs → repos → tags
    log_step("Mapping missing blobs to repositories and tags...")
    repo_tag_rows = []  # (repo_id, repo_full_name, tag_name, digest)
    for digest in missing_digests:
        out = sql(
            f"SELECT DISTINCT r.id, u.username || '/' || r.name, t.name "
            f"FROM repository r "
            f"JOIN public.user u ON r.namespace_user_id = u.id "
            f"JOIN manifest m ON m.repository_id = r.id "
            f"JOIN manifestblob mb ON mb.manifest_id = m.id "
            f"JOIN imagestorage is2 ON mb.blob_id = is2.id "
            f"LEFT JOIN tag t ON t.manifest_id = m.id "
            f"WHERE is2.content_checksum = '{digest}' "
            f"AND (t.hidden = false OR t.hidden IS NULL) "
            f"AND (t.lifetime_end_ms IS NULL OR "
            f"t.lifetime_end_ms > (EXTRACT(EPOCH FROM now()) * 1000));")
        for line in out.splitlines():
            parts = line.strip().split("|")
            if len(parts) >= 3 and parts[0].strip():
                repo_tag_rows.append((parts[0].strip(), parts[1].strip(),
                                      parts[2].strip(), digest))

    # Unique repos
    unique_repos = {}
    for rid, rname, tag, digest in repo_tag_rows:
        if rid not in unique_repos:
            unique_repos[rid] = rname

    log_info(f"Affected repositories: {len(unique_repos)}")

    repos_fixed = repos_failed = tags_restored = tags_failed = 0

    for repo_id, repo_full_name in unique_repos.items():
        print("────────────────────────────────────────")
        log_step(f"Repo: {repo_full_name} (id={repo_id})")

        mirror_url = sql(
            f"SELECT external_reference FROM repomirrorconfig "
            f"WHERE repository_id = {repo_id};", single=True)
        orig_state = sql(
            f"SELECT state FROM repository WHERE id = {repo_id};", single=True)

        tags = sorted(set(tag for rid, _, tag, _ in repo_tag_rows
                          if rid == repo_id and tag))
        repo_digests = sorted(set(d for rid, _, _, d in repo_tag_rows
                                   if rid == repo_id))

        if not tags:
            log_warn("No live tags found — may need manual re-mirror")
            continue

        log_info(f"Mirror source: {mirror_url or 'UNKNOWN'}")
        log_info(f"Tags to re-sync: {len(tags)}")

        if not mirror_url:
            log_warn("No mirror source configured — cannot auto-fix")
            repos_failed += 1
            continue

        # Flip to NORMAL
        log_info("Switching repo to NORMAL...")
        sql(f"UPDATE repository SET state = 0 WHERE id = {repo_id};")

        repo_ok = True
        for tag in tags:
            if not tag:
                continue
            print(f"  Tag {tag}: ", end="", flush=True)

            # Purge stale blob refs
            digest_list_sql = ",".join(f"'{d}'" for d in repo_digests)
            blob_ids_out = sql(
                f"SELECT DISTINCT is2.id FROM imagestorage is2 "
                f"JOIN manifestblob mb ON mb.blob_id = is2.id "
                f"JOIN manifest m ON mb.manifest_id = m.id "
                f"JOIN tag t ON t.manifest_id = m.id "
                f"WHERE t.name = '{tag}' AND m.repository_id = {repo_id} "
                f"AND is2.content_checksum IN ({digest_list_sql});", single=True)
            for blob_id in blob_ids_out.splitlines():
                blob_id = blob_id.strip()
                if blob_id:
                    try:
                        sql(f"BEGIN; "
                            f"DELETE FROM manifestblob WHERE blob_id = {blob_id}; "
                            f"DELETE FROM imagestorageplacement WHERE storage_id = {blob_id}; "
                            f"DELETE FROM uploadedblob WHERE blob_id = {blob_id}; "
                            f"DELETE FROM imagestoragesignature WHERE storage_id = {blob_id}; "
                            f"DELETE FROM imagestorage WHERE id = {blob_id}; "
                            f"COMMIT;")
                    except Exception:
                        pass

            run(f'skopeo delete --authfile {authfile} '
                f'"docker://{quay_host}/{repo_full_name}:{tag}"', check=False)

            rc = subprocess.run(
                ["skopeo", "copy", "--authfile", authfile, "--all",
                 f"docker://{mirror_url}:{tag}",
                 f"docker://{quay_host}/{repo_full_name}:{tag}"],
                capture_output=True, text=True, timeout=600)

            if rc.returncode == 0:
                verify = subprocess.run(
                    ["skopeo", "inspect", "--authfile", authfile,
                     f"docker://{quay_host}/{repo_full_name}:{tag}"],
                    capture_output=True, text=True, timeout=60)
                if verify.returncode == 0:
                    print(f"{GREEN}OK{NC}")
                    tags_restored += 1
                else:
                    print(f"{YELLOW}PUSHED but verify failed{NC}")
                    tags_failed += 1; repo_ok = False
            else:
                print(f"{RED}FAILED{NC}")
                tags_failed += 1; repo_ok = False

        # Flip back
        sql(f"UPDATE repository SET state = {orig_state or 2} WHERE id = {repo_id};")
        if repo_ok:
            log_info("✓ repaired")
            repos_fixed += 1
        else:
            log_warn("partial repair")
            repos_failed += 1
        print()

    print("========================================")
    print("  FIX-MISSING RESULTS")
    print("========================================")
    print(f"  Repos fixed:   {GREEN}{repos_fixed}{NC}")
    print(f"  Repos failed:  {RED}{repos_failed}{NC}")
    print(f"  Tags restored: {GREEN}{tags_restored}{NC}")
    print(f"  Tags failed:   {RED}{tags_failed}{NC}")
    print("========================================")

    if authfile_tmp:
        os.unlink(authfile)

    # Purge orphaned blobs that have no live tags (can't be fix-missing'd)
    repaired_digests = set()
    for _, _, _, digest in repo_tag_rows:
        repaired_digests.add(digest)
    orphaned = [d for d in missing_digests if d not in repaired_digests]
    if orphaned:
        print()
        log_info(f"{len(orphaned)} blobs have no live tags — purging stale DB refs...")
        _fix_purge(quay_ns, pg_pod, db_user, db_name, orphaned, sql)


if __name__ == "__main__":
    main()
