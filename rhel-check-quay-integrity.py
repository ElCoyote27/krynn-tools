#!/usr/bin/env python3
"""
Check Quay blob integrity on RHEL-based Quay (container deployment).

Verifies that blobs referenced in the Quay database actually exist in
S3 storage.  Reports (and optionally repairs) missing blobs that cause
image pull failures.

Connects via SSH to the target host and auto-detects PostgreSQL
container, database, Quay container, and S3 config.

Requirements:
  - SSH access with passwordless sudo to the target host
  - python3 (with PyYAML if available; regex fallback otherwise)
  - boto3 (on remote host, inside Quay container, or locally)

Examples:
  # Scan central quay
  python3 rhel-check-quay-integrity.py cquay1 --org ocp4

  # Purge stale refs (then re-run oc-mirror)
  python3 rhel-check-quay-integrity.py cquay1 --org ocp4 --fix
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import textwrap

# ─── Colors ──────────────────────────────────────────────────────────
RED    = "\033[0;31m"
GREEN  = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN   = "\033[0;36m"
NC     = "\033[0m"

def log_info(msg):  print(f"{GREEN}[INFO]{NC} {msg}", flush=True)
def log_warn(msg):  print(f"{YELLOW}[WARN]{NC} {msg}", flush=True)
def log_error(msg): print(f"{RED}[ERROR]{NC} {msg}", flush=True)
def log_step(msg):  print(f"{CYAN}[STEP]{NC} {msg}", flush=True)


def run(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed ({r.returncode}): {cmd}\n{r.stderr}")
    return r.stdout.strip()


def ssh(host, cmd, check=True):
    return run(f"ssh {host} {repr(cmd)}", check=check)


def ssh_psql(host, pg_ct, db_user, db_name, sql, single=False):
    flags = "-t -A" if single else "-t"
    escaped = sql.replace("'", "'\\''")
    out = ssh(host,
        f"sudo podman exec {pg_ct} psql -U {db_user} -d {db_name} {flags} -c '{escaped}'",
        check=False)
    if single:
        return out.strip()
    return out


# ─── S3 config extraction ───────────────────────────────────────────
def extract_s3_config(config_yaml):
    try:
        import yaml
        cfg = yaml.safe_load(config_yaml)
    except ImportError:
        return _extract_s3_regex(config_yaml)

    dist = cfg.get("DISTRIBUTED_STORAGE_CONFIG", {})
    if not dist:
        raise RuntimeError("DISTRIBUTED_STORAGE_CONFIG not found")

    name = list(dist.keys())[0]
    entry = dist[name]
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

    return {"access_key": ak, "secret_key": sk, "bucket": bucket,
            "path": sp, "endpoint": endpoint}


def _extract_s3_regex(config_yaml):
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


# ─── Checker script (runs on remote or inside container) ────────────
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


def main():
    parser = argparse.ArgumentParser(
        description="Quay Blob Integrity Checker for RHEL-based Quay deployments",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python3 rhel-check-quay-integrity.py cquay1 --org ocp4
              python3 rhel-check-quay-integrity.py cquay1 --org ocp4 --fix

            Note:
              Central Quay repos are NORMAL, not MIRROR — to restore missing
              blobs, purge stale refs with --fix then re-run oc-mirror.
              For regional (OCP) quays, use ocp-check-quay-integrity.py --fix-missing.
        """),
    )
    parser.add_argument("host", help="SSH target (alias, hostname, user@host)")
    parser.add_argument("--org", default=os.environ.get("ORG_NAME", ""),
                        help="Check only this organization")
    parser.add_argument("--list-orgs", action="store_true",
                        help="List organizations and exit")
    parser.add_argument("--fix", action="store_true",
                        help="Purge stale DB refs for missing blobs")
    parser.add_argument("--verbose", action="store_true",
                        help="Show per-blob S3 errors")
    parser.add_argument("--batch-size", type=int,
                        default=int(os.environ.get("BATCH_SIZE", "500")),
                        help="Blobs per progress batch (default: 500)")
    args = parser.parse_args()

    ssh_host = args.host

    print()
    print("=== Quay Blob Integrity Check (RHEL) ===")
    print()

    # ─── Verify SSH ─────────────────────────────────────────────────
    try:
        ssh(ssh_host, "true")
    except Exception:
        log_error(f"Cannot SSH to {ssh_host}")
        sys.exit(1)
    log_info(f"SSH access: OK ({ssh_host})")

    # ─── Detect PostgreSQL container ────────────────────────────────
    log_info("Detecting PostgreSQL container...")
    pg_ct = ssh(ssh_host,
        "sudo podman ps --format '{{.Names}}' "
        "| grep -iE 'postgresql-quay|postgres.*quay' | head -1", check=False)
    if not pg_ct:
        pg_ct = ssh(ssh_host,
            "sudo podman ps --format '{{.Names}}' "
            "| grep -iE 'postgres|pgsql|postgresql' | grep -v clair | head -1",
            check=False)
    if not pg_ct:
        log_error(f"Could not find PostgreSQL container on {ssh_host}")
        sys.exit(1)
    log_info(f"PostgreSQL container: {pg_ct}")

    # ─── Detect database ───────────────────────────────────────────
    db_name_out = ssh(ssh_host,
        f"sudo podman exec {pg_ct} psql -U postgres -t "
        f"-c 'SELECT datname FROM pg_database;'", check=False)
    db_name = ""
    for line in db_name_out.splitlines():
        if "quay" in line.lower():
            db_name = line.strip(); break
    if not db_name:
        db_name = "quay"

    db_user_out = ssh(ssh_host,
        f"sudo podman exec {pg_ct} psql -U postgres -t "
        f"-c 'SELECT usename FROM pg_user;'", check=False)
    db_user = ""
    for line in db_user_out.splitlines():
        if "quay" in line.lower():
            db_user = line.strip(); break
    if not db_user:
        db_user = "quayuser"
    log_info(f"Database: {db_name} (user: {db_user})")

    def sql(query, single=False):
        return ssh_psql(ssh_host, pg_ct, db_user, db_name, query, single=single)

    # ─── List orgs ──────────────────────────────────────────────────
    if args.list_orgs:
        log_info(f"Organizations in {db_name}:")
        print(sql('SELECT username FROM public."user" WHERE organization = true ORDER BY username;'))
        sys.exit(0)

    # ─── Detect Quay container ──────────────────────────────────────
    quay_ct = ssh(ssh_host,
        "sudo podman ps --format '{{.Names}}' "
        "| grep -iE '^quay$|quay-app|quay-registry' "
        "| grep -v postgres | grep -v redis | grep -v clair | head -1",
        check=False)
    if not quay_ct:
        quay_ct = ssh(ssh_host,
            "sudo podman ps --format '{{.Names}} {{.Image}}' "
            "| grep -iE 'quay/quay-rhel[89]' | awk '{print $1}' | head -1",
            check=False)

    # ─── Extract S3 config ──────────────────────────────────────────
    log_step("Extracting S3 configuration from Quay...")
    config_text = ""
    if quay_ct:
        for cpath in ("/conf/stack/config.yaml",
                      "/quay-registry/conf/stack/config.yaml",
                      "/config/config.yaml"):
            config_text = ssh(ssh_host,
                f"sudo podman exec {quay_ct} cat {cpath}", check=False)
            if config_text:
                break
    if not config_text:
        for hpath in ("/data/config/config.yaml",
                      "/opt/quay/config/config.yaml",
                      "/etc/quay/config.yaml"):
            config_text = ssh(ssh_host, f"sudo cat {hpath}", check=False)
            if config_text:
                break
    if not config_text:
        log_error(f"Could not read Quay config from {ssh_host}")
        sys.exit(1)

    s3 = extract_s3_config(config_text)
    if not s3["access_key"] or not s3["secret_key"]:
        log_error("S3 access_key or secret_key is empty")
        sys.exit(1)

    log_info(f"S3 Bucket:   {s3['bucket']}")
    log_info(f"S3 Path:     {s3['path']}")
    log_info(f"S3 Endpoint: {s3['endpoint']}")

    # ─── Build org filter ───────────────────────────────────────────
    org_name = args.org
    if org_name:
        org_id = sql(f"SELECT id FROM public.\"user\" WHERE username = '{org_name}';",
                     single=True)
        if not org_id:
            log_error(f"Organization '{org_name}' not found")
            sys.exit(1)
        log_info(f"Organization: {org_name} (ID: {org_id})")
        org_filter = (f"JOIN manifestblob mb ON mb.blob_id = is2.id "
                      f"JOIN manifest m ON mb.manifest_id = m.id "
                      f"JOIN repository r ON m.repository_id = r.id "
                      f"WHERE r.namespace_user_id = {org_id} AND")
    else:
        log_info("Organization: ALL")
        org_filter = "WHERE"

    # ─── Count and dump blobs ───────────────────────────────────────
    log_step("Counting blobs to verify...")
    blob_count = int(sql(
        f"SELECT count(DISTINCT is2.id) FROM imagestorage is2 "
        f"{org_filter} is2.content_checksum IS NOT NULL;", single=True) or "0")

    print(f"\n========================================")
    print(f"  Blobs to verify: {blob_count}")
    print(f"========================================\n")

    if blob_count == 0:
        log_info("No blobs to check.")
        sys.exit(0)

    log_step("Extracting blob digests from database...")
    digest_output = sql(
        f"SELECT DISTINCT is2.content_checksum FROM imagestorage is2 "
        f"{org_filter} is2.content_checksum IS NOT NULL;")
    digest_lines = [l.strip() for l in digest_output.splitlines()
                    if l.strip().startswith("sha256:")]
    actual_count = len(digest_lines)
    log_info(f"Extracted {actual_count} unique digests")

    # Write temp files
    digest_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    digest_file.write("\n".join(digest_lines) + "\n")
    digest_file.close()

    check_file = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
    check_file.write(CHECKER_SCRIPT)
    check_file.close()

    creds_data = {
        "access_key": s3["access_key"], "secret_key": s3["secret_key"],
        "bucket": s3["bucket"], "path": s3["path"],
        "endpoint": s3["endpoint"],
        "batch_size": args.batch_size, "verbose": args.verbose,
    }
    creds_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(creds_data, creds_file)
    creds_file.close()

    # ─── Decide where to run the check ──────────────────────────────
    log_step("Running integrity check...")
    raw_output = ""

    try:
        # Prefer remote host with boto3
        has_boto3 = ssh(ssh_host, "python3 -c 'import boto3'", check=False)
        if has_boto3 is not None and subprocess.run(
                f"ssh {ssh_host} python3 -c 'import boto3'",
                shell=True, capture_output=True).returncode == 0:
            log_info("Running check on remote host")
            run(f"scp -q {check_file.name} {ssh_host}:/tmp/_integrity_check.py")
            run(f"scp -q {digest_file.name} {ssh_host}:/tmp/_integrity_digests.txt")
            run(f"scp -q {creds_file.name} {ssh_host}:/tmp/_integrity_creds.json")
            raw_output = ssh(ssh_host,
                "cat /tmp/_integrity_digests.txt | python3 /tmp/_integrity_check.py",
                check=False)

        elif quay_ct and subprocess.run(
                f"ssh {ssh_host} 'sudo podman exec {quay_ct} python3 -c \"import boto3\"'",
                shell=True, capture_output=True).returncode == 0:
            log_info("Running check inside Quay container")
            run(f"scp -q {check_file.name} {ssh_host}:/tmp/_integrity_check.py")
            run(f"scp -q {digest_file.name} {ssh_host}:/tmp/_integrity_digests.txt")
            run(f"scp -q {creds_file.name} {ssh_host}:/tmp/_integrity_creds.json")
            ssh(ssh_host, f"sudo podman cp /tmp/_integrity_check.py {quay_ct}:/tmp/")
            ssh(ssh_host, f"sudo podman cp /tmp/_integrity_digests.txt {quay_ct}:/tmp/")
            ssh(ssh_host, f"sudo podman cp /tmp/_integrity_creds.json {quay_ct}:/tmp/")
            raw_output = ssh(ssh_host,
                f"sudo podman exec {quay_ct} bash -c "
                f"'cat /tmp/_integrity_digests.txt | python3 /tmp/_integrity_check.py'",
                check=False)

        elif subprocess.run("python3 -c 'import boto3'",
                            shell=True, capture_output=True).returncode == 0:
            log_info("Running check locally")
            r = subprocess.run(
                f"cat {digest_file.name} | python3 {check_file.name}",
                shell=True, capture_output=True, text=True, timeout=7200,
                env={**os.environ})
            raw_output = r.stdout.strip()
        else:
            log_error("python3+boto3 not available locally, on remote host, or in Quay container")
            log_info("Install: pip3 install boto3")
            sys.exit(1)
    finally:
        os.unlink(check_file.name)
        os.unlink(digest_file.name)
        os.unlink(creds_file.name)

    # ─── Parse results ──────────────────────────────────────────────
    for line in raw_output.splitlines():
        if line.startswith("PROGRESS "):
            parts = line.split()
            if len(parts) >= 4:
                print(f"\r  Checked: {parts[1]} / {actual_count} | "
                      f"Missing: {parts[2]} | Errors: {parts[3]}",
                      end="", flush=True)
    print()

    results_json = None
    capture = False
    for line in raw_output.splitlines():
        if line.strip() == "RESULTS_START":
            capture = True; continue
        if line.strip() == "RESULTS_END":
            capture = False; continue
        if capture:
            try:
                results_json = json.loads(line)
            except json.JSONDecodeError:
                pass

    if not results_json:
        log_error("Failed to get results from integrity check")
        print(raw_output)
        sys.exit(1)

    total_checked = results_json["checked"]
    total_ok      = results_json["ok"]
    total_missing = results_json["missing"]
    total_errors  = results_json["errors"]
    missing_digests = results_json.get("missing_digests", [])

    print(f"\n========================================")
    print(f"  INTEGRITY CHECK RESULTS")
    print(f"========================================")
    print(f"  Checked:  {CYAN}{total_checked}{NC}")
    print(f"  OK:       {GREEN}{total_ok}{NC}")
    print(f"  Missing:  {RED}{total_missing}{NC}")
    print(f"  Errors:   {YELLOW}{total_errors}{NC}")
    print(f"========================================")

    if total_missing == 0 and total_errors == 0:
        print()
        log_info("All blobs verified successfully ✓")
        sys.exit(0)

    # ─── List missing blobs ─────────────────────────────────────────
    if total_missing > 0:
        print()
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

    # ─── Fix mode ───────────────────────────────────────────────────
    if args.fix and total_missing > 0:
        print()
        log_step("Purging stale DB references for missing blobs...")
        fixed = 0
        for digest in missing_digests:
            blob_id = sql(f"SELECT id FROM imagestorage WHERE content_checksum = '{digest}';",
                          single=True)
            if blob_id:
                try:
                    sql(f"BEGIN; "
                        f"DELETE FROM manifestblob WHERE blob_id = {blob_id}; "
                        f"DELETE FROM imagestorageplacement WHERE storage_id = {blob_id}; "
                        f"DELETE FROM uploadedblob WHERE blob_id = {blob_id}; "
                        f"DELETE FROM imagestoragesignature WHERE storage_id = {blob_id}; "
                        f"DELETE FROM imagestorage WHERE id = {blob_id}; "
                        f"COMMIT;")
                    log_info(f"Purged: {digest}")
                    fixed += 1
                except Exception as e:
                    log_warn(f"Failed to purge: {digest} — {e}")
        print()
        log_info(f"Purged {fixed} / {total_missing} stale blob references")
        log_info("Re-run oc-mirror to restore the actual blob data")

    elif total_missing > 0:
        print()
        log_warn("Use --fix to purge stale DB references, then re-run oc-mirror")

    from datetime import datetime
    save_file = f"/tmp/quay-integrity-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    with open(save_file, "w") as f:
        json.dump(results_json, f, indent=2)
    log_info(f"Full results saved to: {save_file}")


if __name__ == "__main__":
    main()
