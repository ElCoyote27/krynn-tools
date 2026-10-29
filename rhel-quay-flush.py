#!/usr/bin/env python3
"""
Flush organization repositories from RHEL-based Quay (container deployment).

Connects via SSH and auto-detects PostgreSQL container, database, Quay
container, and S3 config.  Default is dry-run; pass --execute to delete.

Usage:
  python3 rhel-flush-quay.py cquay1                         # dry-run
  python3 rhel-flush-quay.py cquay1 --org ocp4 --execute     # delete repos
  python3 rhel-flush-quay.py cquay1 --org ocp4 --execute --s3-cleanup

Requirements:
  - SSH access with passwordless sudo to the target host
  - For --s3-cleanup: aws CLI (locally or on remote host)
"""

import argparse
import json
import os
import subprocess
import sys

RED    = "\033[0;31m"
GREEN  = "\033[0;32m"
YELLOW = "\033[1;33m"
NC     = "\033[0m"

def log_info(msg):  print(f"{GREEN}[INFO]{NC} {msg}", flush=True)
def log_warn(msg):  print(f"{YELLOW}[WARN]{NC} {msg}", flush=True)
def log_error(msg): print(f"{RED}[ERROR]{NC} {msg}", flush=True)


def run(cmd, check=True, timeout=300):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed ({r.returncode}): {cmd}\n{r.stderr}")
    return r.stdout.strip()


def ssh(host, cmd, check=True):
    return run(f"ssh {host} {repr(cmd)}", check=check)


def ssh_psql(host, pg_ct, db_user, db_name, query, single=False):
    flags = "-t -A" if single else "-t"
    escaped = query.replace("'", "'\\''")
    out = ssh(host,
        f"sudo podman exec {pg_ct} psql -U {db_user} -d {db_name} {flags} -c '{escaped}'",
        check=False)
    return out.strip() if single else out


def extract_s3_config(config_yaml):
    """Parse Quay config.yaml and return S3 storage parameters."""
    try:
        import yaml
        cfg = yaml.safe_load(config_yaml)
    except ImportError:
        return _extract_s3_regex(config_yaml)

    dist = cfg.get("DISTRIBUTED_STORAGE_CONFIG", {})
    if not dist:
        return None
    name = list(dist.keys())[0]
    entry = dist[name]
    params = entry[1] if isinstance(entry, list) and len(entry) > 1 else entry
    if not isinstance(params, dict):
        return None

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


def read_quay_config(ssh_host):
    """Read config.yaml from the Quay container or host paths."""
    # Find quay container
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

    return config_text


def main():
    parser = argparse.ArgumentParser(
        description="Flush organization repositories from RHEL-based Quay")
    parser.add_argument("host", help="SSH target (alias, hostname, user@host)")
    parser.add_argument("--org", default=os.environ.get("ORG_NAME", "ocp4"),
                        help="Organization to flush (default: ocp4)")
    parser.add_argument("--list-orgs", action="store_true",
                        help="List organizations and exit")
    parser.add_argument("--execute", action="store_true",
                        help="Execute flush (default: dry-run)")
    parser.add_argument("--s3-cleanup", action="store_true",
                        help="Also cleanup orphaned S3 blobs")
    args = parser.parse_args()

    ssh_host = args.host
    org_name = args.org

    print(f"=== Quay {org_name} Organization Flush (RHEL) ===")
    print()
    log_info(f"Host: {ssh_host}")
    print()

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

    # Detect database
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
    log_info(f"Database: {db_name}, User: {db_user}")

    def sql(query, single=False):
        return ssh_psql(ssh_host, pg_ct, db_user, db_name, query, single=single)

    # ─── List orgs ──────────────────────────────────────────────────
    if args.list_orgs:
        log_info(f"Organizations in {db_name}:")
        print(sql('SELECT username FROM "user" WHERE organization = true ORDER BY username;'))
        sys.exit(0)

    # Get org ID
    org_id = sql(f"SELECT id FROM \"user\" WHERE username = '{org_name}';", single=True)
    if not org_id:
        log_error(f"Organization '{org_name}' not found")
        log_info("Available organizations:")
        print(sql('SELECT username FROM "user" WHERE organization = true;'))
        sys.exit(1)

    log_info(f"Organization: {org_name} (ID: {org_id})")
    print()

    repo_count = int(sql(f"SELECT COUNT(*) FROM repository WHERE namespace_user_id = {org_id};",
                         single=True) or "0")
    quota_bytes = int(sql(
        f"SELECT COALESCE(size_bytes, 0) FROM quotanamespacesize "
        f"WHERE namespace_user_id = {org_id};", single=True) or "0")

    print(f"Repositories: {repo_count}")
    print(f"Quota: {quota_bytes // 1024**3} GB ({quota_bytes} bytes)")

    if not args.execute:
        print()
        print(f"[DRY-RUN] Use --execute to delete {repo_count} repositories and reset quota")
        if args.s3_cleanup:
            print("          --s3-cleanup will auto-detect S3 config from Quay")
        sys.exit(0)

    print()

    # ─── Delete repositories ────────────────────────────────────────
    if repo_count > 0:
        print("=== DELETING REPOSITORIES ===")
        tables = ["tag", "manifestchild", "manifestlabel", "manifestsecuritystatus",
                   "manifestblob", "manifest", "uploadedblob", "quotarepositorysize",
                   "repositorysearchscore", "repositorypermission",
                   "repositoryactioncount", "blobupload", "accesstoken"]
        for table in tables:
            print(f"Deleting from {table}... ", end="", flush=True)
            result = sql(f"DELETE FROM {table} WHERE repository_id IN "
                         f"(SELECT id FROM repository WHERE namespace_user_id = {org_id});")
            print("OK" if "DELETE" in result else "SKIP")

        print("Deleting repositories... ", end="", flush=True)
        sql(f"DELETE FROM repository WHERE namespace_user_id = {org_id};")
        remaining = sql(f"SELECT COUNT(*) FROM repository WHERE namespace_user_id = {org_id};",
                        single=True)
        print(f"Remaining: {remaining}")
    else:
        print("No repositories to delete.")

    print()
    print("=== RESETTING QUOTA ===")
    sql(f"UPDATE quotanamespacesize SET size_bytes = 0 WHERE namespace_user_id = {org_id};")
    new_quota = sql(f"SELECT COALESCE(size_bytes, 0) FROM quotanamespacesize "
                    f"WHERE namespace_user_id = {org_id};", single=True)
    print(f"New quota: {new_quota} bytes")

    # ─── S3 cleanup ─────────────────────────────────────────────────
    if args.s3_cleanup:
        print()
        print("=== S3 CLEANUP ===")

        config_text = read_quay_config(ssh_host)
        if not config_text:
            log_error(f"Could not read Quay config from {ssh_host}")
            sys.exit(1)

        s3 = extract_s3_config(config_text)
        if not s3 or not s3["bucket"] or not s3["endpoint"]:
            log_error("S3 config incomplete")
            sys.exit(1)

        log_info(f"S3 Bucket:   {s3['bucket']}")
        log_info(f"S3 Path:     {s3['path']}")
        log_info(f"S3 Endpoint: {s3['endpoint']}")

        # Find aws CLI
        aws_cmd = ""
        if subprocess.run("command -v aws", shell=True, capture_output=True).returncode == 0:
            aws_cmd = "aws"
            log_info("Using local aws CLI")
        elif subprocess.run(f"ssh {ssh_host} command -v aws",
                            shell=True, capture_output=True).returncode == 0:
            aws_cmd = f"ssh {ssh_host} aws"
            log_info(f"Using aws CLI on {ssh_host}")
        else:
            log_error("aws CLI not found locally or on remote host")
            sys.exit(1)

        env = {**os.environ,
               "AWS_ACCESS_KEY_ID": s3["access_key"],
               "AWS_SECRET_ACCESS_KEY": s3["secret_key"]}

        print("Identifying blobs to keep...")
        keep_out = sql("SELECT DISTINCT content_checksum FROM imagestorage "
                       "WHERE id IN (SELECT storage_id FROM manifestblob "
                       "UNION SELECT storage_id FROM uploadedblob);")
        keep_hashes = set()
        for line in keep_out.splitlines():
            h = line.strip().replace("sha256:", "")
            if h:
                keep_hashes.add(h)
        print(f"Blobs to keep: {len(keep_hashes)}")

        print("Listing S3 objects...")
        s3_out = run(f"{aws_cmd} s3 ls 's3://{s3['bucket']}/{s3['path']}/sha256/' --recursive "
                     f"--endpoint-url '{s3['endpoint']}' --no-verify-ssl", check=False)
        s3_paths = []
        s3_hashes = set()
        for line in s3_out.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3]
                s3_paths.append(path)
                s3_hashes.add(path.rsplit("/", 1)[-1])
        print(f"Total S3 objects: {len(s3_paths)}")

        orphan_hashes = s3_hashes - keep_hashes
        print(f"Orphaned blobs: {len(orphan_hashes)}")

        if not orphan_hashes:
            print("No orphaned blobs to delete.")
        else:
            delete_paths = [p for p in s3_paths if p.rsplit("/", 1)[-1] in orphan_hashes]
            print(f"Deleting {len(delete_paths)} orphaned objects...")
            deleted = 0
            for path in delete_paths:
                run(f"{aws_cmd} s3 rm 's3://{s3['bucket']}/{path}' "
                    f"--endpoint-url '{s3['endpoint']}' --no-verify-ssl", check=False)
                deleted += 1
                if deleted % 500 == 0:
                    print(f"  Deleted {deleted} / {len(delete_paths)}", flush=True)
            print(f"  Deleted {deleted} objects total")

            run(f"{aws_cmd} s3 rm 's3://{s3['bucket']}/{s3['path']}/uploads/' --recursive "
                f"--endpoint-url '{s3['endpoint']}' --no-verify-ssl", check=False)
            log_info("Upload objects cleaned")
    else:
        print()
        print("=== DONE ===")
        print("NOTE: S3 blobs not deleted. Use --s3-cleanup to clean orphaned blobs.")


if __name__ == "__main__":
    main()
