#!/usr/bin/env python3
"""
Flush organization repositories from OCP-based Quay (operator deployment).

Kubeconfig, namespace, database pod, user, and S3 config are all
auto-detected.  Default is dry-run; pass --execute to actually delete.

Usage:
  export KUBECONFIG=/path/to/kubeconfig
  python3 ocp-flush-quay.py                          # dry-run
  python3 ocp-flush-quay.py --org ocp4 --execute     # delete repos
  python3 ocp-flush-quay.py --org ocp4 --execute --s3-cleanup

Requirements:
  - oc CLI in PATH
  - For --s3-cleanup: aws CLI (locally or via a NooBaa pod)
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile

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


def oc(args, check=True):
    return run(f"oc {args}", check=check)


def oc_exec(ns, pod, cmd, check=True):
    return run(f"oc exec -n {ns} {pod} -- bash -c {repr(cmd)}", check=check)


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


def main():
    parser = argparse.ArgumentParser(
        description="Flush organization repositories from OCP-based Quay")
    parser.add_argument("--org", default=os.environ.get("ORG_NAME", "ocp4"),
                        help="Organization to flush (default: ocp4)")
    parser.add_argument("--namespace", default=os.environ.get("QUAY_NAMESPACE", ""),
                        help="Quay namespace (auto-detected)")
    parser.add_argument("--list-orgs", action="store_true",
                        help="List organizations and exit")
    parser.add_argument("--execute", action="store_true",
                        help="Execute flush (default: dry-run)")
    parser.add_argument("--s3-cleanup", action="store_true",
                        help="Also cleanup orphaned S3/ODF blobs")
    args = parser.parse_args()

    org_name = args.org
    quay_ns = args.namespace

    print(f"=== Quay {org_name} Organization Flush (OCP) ===")
    print()

    # ─── Cluster access ─────────────────────────────────────────────
    try:
        oc("cluster-info")
    except Exception:
        log_error("Cannot connect to cluster. Check $KUBECONFIG.")
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
            log_error("Could not auto-detect Quay namespace")
            sys.exit(1)
    log_info(f"Namespace: {quay_ns}")

    # ─── Detect PostgreSQL pod ──────────────────────────────────────
    log_info("Detecting PostgreSQL pod...")
    pg_pod = oc(f"get pods -n {quay_ns} -l quay-component=postgres "
                f"-o jsonpath='{{.items[0].metadata.name}}'", check=False)
    if not pg_pod:
        out = oc(f"get pods -n {quay_ns} -o name", check=False)
        for line in out.splitlines():
            if ("database" in line.lower() or "postgres" in line.lower()) \
                    and "clair" not in line.lower():
                pg_pod = line.replace("pod/", "").strip(); break
    if not pg_pod:
        log_error(f"Could not find PostgreSQL pod in {quay_ns}")
        sys.exit(1)
    log_info(f"PostgreSQL pod: {pg_pod}")

    # ─── Detect DB user/name ────────────────────────────────────────
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
    print()

    def sql(query, single=False):
        flags = "-t -A" if single else "-t"
        escaped = query.replace("'", "'\\''")
        out = oc_exec(quay_ns, pg_pod,
            f"psql -U {db_user} -d {db_name} {flags} -c '{escaped}'", check=False)
        return out.strip() if single else out

    # ─── List orgs ──────────────────────────────────────────────────
    if args.list_orgs:
        log_info(f"Organizations in {db_name}:")
        print(sql("SELECT username FROM public.user WHERE organization = true ORDER BY username;"))
        sys.exit(0)

    # Get org ID
    org_id = sql(f"SELECT id FROM public.user WHERE username = '{org_name}';", single=True)
    if not org_id:
        log_error(f"Organization '{org_name}' not found")
        log_info("Available organizations:")
        print(sql("SELECT username FROM public.user WHERE organization = true;"))
        sys.exit(1)
    log_info(f"Organization: {org_name} (ID: {org_id})")
    print()

    repo_count = int(sql(f"SELECT COUNT(*) FROM repository WHERE namespace_user_id = {org_id};",
                         single=True) or "0")
    quota_bytes = int(sql(
        f"SELECT COALESCE(size_bytes, 0) FROM quotanamespacesize "
        f"WHERE namespace_user_id = {org_id};", single=True) or "0")
    quota_gb = quota_bytes // (1024**3)

    print(f"Repositories: {repo_count}")
    print(f"Quota: {quota_gb} GB ({quota_bytes} bytes)")

    if not args.execute:
        print()
        print(f"[DRY-RUN] Use --execute to delete {repo_count} repositories and reset quota")
        if args.s3_cleanup:
            print("          --s3-cleanup will auto-detect ODF/S3 config from Quay")
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

    # ─── Reset quota ────────────────────────────────────────────────
    print()
    print("=== RESETTING QUOTA ===")
    sql(f"UPDATE quotanamespacesize SET size_bytes = 0 WHERE namespace_user_id = {org_id};")
    new_quota = sql(f"SELECT COALESCE(size_bytes, 0) FROM quotanamespacesize "
                    f"WHERE namespace_user_id = {org_id};", single=True)
    print(f"New quota: {new_quota} bytes")

    # ─── S3 cleanup ─────────────────────────────────────────────────
    if args.s3_cleanup:
        print()
        print("=== ODF/S3 STORAGE CLEANUP ===")

        s3_bucket = s3_endpoint = s3_path = ""
        aws_ak = aws_sk = ""

        # Try OBC first
        log_info("Checking for ObjectBucketClaim (ODF-managed storage)...")
        obc_name = oc(f"get obc -n {quay_ns} -o jsonpath='{{.items[0].metadata.name}}'",
                      check=False)
        if obc_name:
            log_info(f"Found ObjectBucketClaim: {obc_name}")
            s3_bucket = oc(f"get cm {obc_name} -n {quay_ns} "
                           f"-o jsonpath='{{.data.BUCKET_NAME}}'", check=False)
            bucket_host = oc(f"get cm {obc_name} -n {quay_ns} "
                             f"-o jsonpath='{{.data.BUCKET_HOST}}'", check=False)
            aws_ak_b64 = oc(f"get secret {obc_name} -n {quay_ns} "
                            f"-o jsonpath='{{.data.AWS_ACCESS_KEY_ID}}'", check=False)
            aws_sk_b64 = oc(f"get secret {obc_name} -n {quay_ns} "
                            f"-o jsonpath='{{.data.AWS_SECRET_ACCESS_KEY}}'", check=False)
            if aws_ak_b64:
                aws_ak = base64.b64decode(aws_ak_b64).decode()
            if aws_sk_b64:
                aws_sk = base64.b64decode(aws_sk_b64).decode()

            s3_route = oc("get route s3 -n openshift-storage "
                          "-o jsonpath='{.spec.host}'", check=False)
            if s3_route:
                s3_endpoint = f"https://{s3_route}"
            elif bucket_host:
                s3_endpoint = f"https://{bucket_host}"
            s3_path = "datastorage/registry"

        # Fallback: Quay config secret
        if not s3_bucket or not s3_endpoint:
            log_info("Extracting S3 config from Quay config secret...")
            config_secret = ""
            secrets_out = oc(f"get secrets -n {quay_ns} -o name", check=False)
            for line in secrets_out.splitlines():
                name = line.replace("secret/", "").strip()
                if "quay" in name.lower() and "config" in name.lower() \
                        and "clair" not in name.lower() and "postgres" not in name.lower():
                    config_secret = name; break
            if not config_secret:
                config_secret = oc(f"get quayregistry -n {quay_ns} "
                                   f"-o jsonpath='{{.items[0].spec.configBundleSecret}}'",
                                   check=False)
            if config_secret:
                config_b64 = oc(f"get secret {config_secret} -n {quay_ns} "
                                f"-o jsonpath='{{.data.config\\.yaml}}'", check=False)
                if config_b64:
                    config_yaml = base64.b64decode(config_b64).decode()
                    s3_cfg = extract_s3_config(config_yaml)
                    if s3_cfg:
                        s3_bucket = s3_bucket or s3_cfg["bucket"]
                        s3_path = s3_path or s3_cfg["path"]
                        s3_endpoint = s3_endpoint or s3_cfg["endpoint"]
                        aws_ak = aws_ak or s3_cfg["access_key"]
                        aws_sk = aws_sk or s3_cfg["secret_key"]

        if not s3_bucket or not s3_endpoint:
            log_error("Could not detect S3/ODF storage configuration")
            sys.exit(1)

        log_info(f"S3 Bucket:   {s3_bucket}")
        log_info(f"S3 Path:     {s3_path}")
        log_info(f"S3 Endpoint: {s3_endpoint}")

        # Find aws CLI
        aws_cmd = ""
        if subprocess.run("command -v aws", shell=True, capture_output=True).returncode == 0:
            aws_cmd = "aws"
            log_info("Using local aws CLI")
        else:
            noobaa_ep = oc("get pods -n openshift-storage -l app=noobaa-endpoint "
                           "-o jsonpath='{.items[0].metadata.name}'", check=False)
            if noobaa_ep:
                aws_cmd = f"oc exec -n openshift-storage {noobaa_ep} -- aws"
                log_info(f"Using NooBaa endpoint pod: {noobaa_ep}")
            else:
                log_error("aws CLI not available. Install: pip3 install awscli")
                sys.exit(1)

        # Set credentials in environment for aws CLI
        env = {**os.environ, "AWS_ACCESS_KEY_ID": aws_ak, "AWS_SECRET_ACCESS_KEY": aws_sk}

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

        print("Listing S3 objects (this may take a while)...")
        s3_out = run(f"{aws_cmd} s3 ls 's3://{s3_bucket}/{s3_path}/sha256/' --recursive "
                     f"--endpoint-url '{s3_endpoint}' --no-verify-ssl", check=False)
        s3_paths = []
        s3_hashes = set()
        for line in s3_out.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3]
                s3_paths.append(path)
                h = path.rsplit("/", 1)[-1] if "/" in path else path
                s3_hashes.add(h)
        print(f"Total S3 objects: {len(s3_paths)}")

        orphan_hashes = s3_hashes - keep_hashes
        print(f"Orphaned blobs: {len(orphan_hashes)}")

        if not orphan_hashes:
            print("No orphaned blobs to delete.")
        else:
            delete_paths = [p for p in s3_paths if p.rsplit("/", 1)[-1] in orphan_hashes]
            print(f"Starting S3 deletion ({len(delete_paths)} objects)...")
            deleted = 0
            for path in delete_paths:
                run(f"{aws_cmd} s3 rm 's3://{s3_bucket}/{path}' "
                    f"--endpoint-url '{s3_endpoint}' --no-verify-ssl", check=False)
                deleted += 1
                if deleted % 500 == 0:
                    print(f"  Deleted {deleted} / {len(delete_paths)}", flush=True)
            print(f"  Deleted {deleted} objects total")

            # Clean uploads
            run(f"{aws_cmd} s3 rm 's3://{s3_bucket}/{s3_path}/uploads/' --recursive "
                f"--endpoint-url '{s3_endpoint}' --no-verify-ssl", check=False)
            log_info("Upload objects cleaned")
    else:
        print()
        print("=== DONE ===")
        print("NOTE: ODF/S3 blobs not cleaned. Use --s3-cleanup to remove orphaned blobs.")


if __name__ == "__main__":
    main()
