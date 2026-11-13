#!/usr/bin/env python3
"""
Reclaim S3 space on a RHEL Quay without deleting live repositories.

Quay's own garbage collector only removes a blob after it deletes an
expired tag or upload in a repository. Blobs that already have no
manifestblob or uploadedblob row are never visited, so they stay in S3.

This tool deletes only those leftovers:

  * imagestorage rows not referenced by manifestblob, uploadedblob, or apprblob
  * S3 sha256 objects whose digest is no longer in the database

Tags, manifests, linked blobs, and the uploads/ prefix are left in place.

Usage:
  python3 rhel-quay-s3-gc.py cquay3
  python3 rhel-quay-s3-gc.py cquay3 --delete --force

cquay0 is treated as production and refuses --delete unless --allow-prod.
"""

import argparse
import json
import subprocess
import sys
import threading

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
NC = "\033[0m"

# Docker's empty layer. Quay refuses to delete this even when unreferenced.
EMPTY_LAYER = "a3ed95caeb02ffe68cdd9fd84406680ae93d633cb16422d00e8a7c22955b46d4"
PROD_HOSTS = {"cquay0"}

ORPHAN_WHERE = f"""
NOT EXISTS (SELECT 1 FROM manifestblob m WHERE m.blob_id = i.id)
AND NOT EXISTS (SELECT 1 FROM uploadedblob u WHERE u.blob_id = i.id)
AND NOT EXISTS (SELECT 1 FROM apprblob a WHERE a.digest = i.content_checksum)
AND COALESCE(i.content_checksum, '') <> 'sha256:{EMPTY_LAYER}'
AND NOT EXISTS (
    SELECT 1 FROM imagestorage i2
    WHERE i2.content_checksum = i.content_checksum
      AND i2.id <> i.id
      AND (
          EXISTS (SELECT 1 FROM manifestblob m WHERE m.blob_id = i2.id)
          OR EXISTS (SELECT 1 FROM uploadedblob u WHERE u.blob_id = i2.id)
      )
)
"""


def log_info(msg):
    print(f"{GREEN}[INFO]{NC} {msg}", flush=True)


def log_warn(msg):
    print(f"{YELLOW}[WARN]{NC} {msg}", flush=True)


def log_error(msg):
    print(f"{RED}[ERROR]{NC} {msg}", flush=True)


def human_size(num):
    num = int(num or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if num < 1024 or unit == "TiB":
            return f"{num:.2f} {unit}" if unit != "B" else f"{num} B"
        num /= 1024
    return f"{num:.2f} TiB"


def ssh(host, cmd, check=True, timeout=300, input_text=None):
    r = subprocess.run(
        ["ssh", host, cmd],
        input=input_text, capture_output=True, text=True, timeout=timeout,
    )
    ssh.last_stderr = (r.stderr or "").strip()
    if check and r.returncode != 0:
        raise RuntimeError(
            f"ssh {host} failed ({r.returncode}): {cmd}\n{ssh.last_stderr}\n{(r.stdout or '').strip()}")
    return r


ssh.last_stderr = ""


def psql(host, pg_ct, query, timeout=300):
    if not query.endswith("\n"):
        query += "\n"
    remote = (
        f"sudo podman exec -i {pg_ct} psql -U quayuser -d quay "
        f"-t -A -v ON_ERROR_STOP=1"
    )
    r = ssh(host, remote, check=True, timeout=timeout, input_text=query)
    return (r.stdout or "").strip()


def detect_container(host, primary_re, fallback_re):
    cmd = (
        "sudo podman ps --format '{{.Names}} {{.Image}}' | "
        f"grep -iE '{primary_re}' | grep -v clair | awk '{{print $1}}' | head -1"
    )
    r = ssh(host, cmd, check=False)
    name = (r.stdout or "").strip().splitlines()
    name = name[0].strip() if name else ""
    if name:
        return name
    cmd = (
        "sudo podman ps --format '{{.Names}} {{.Image}}' | "
        f"grep -iE '{fallback_re}' | grep -v clair | awk '{{print $1}}' | head -1"
    )
    r = ssh(host, cmd, check=False)
    lines = (r.stdout or "").strip().splitlines()
    return lines[0].strip() if lines else ""


def stream_remote_python(host, quay_ct, script, timeout=900):
    """Run script on stdin inside the Quay container. Yield stdout lines."""
    remote = f"sudo podman exec -i -e PYTHONWARNINGS=ignore {quay_ct} python3 -"
    proc = subprocess.Popen(
        ["ssh", host, remote],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    err_box = []

    def drain():
        err_box.append(proc.stderr.read())

    thread = threading.Thread(target=drain)
    thread.start()
    proc.stdin.write(script)
    proc.stdin.close()
    try:
        for line in proc.stdout:
            yield line.rstrip("\n")
    finally:
        proc.wait(timeout=timeout)
        thread.join(timeout=5)
    if proc.returncode != 0:
        err = (err_box[0] if err_box else "") or ""
        raise RuntimeError(
            f"remote python failed ({proc.returncode}): {err.strip()[-2000:]}")


SCANNER = r'''
import json, os, sys
from datetime import datetime, timezone
import boto3
from botocore.client import Config
import urllib3
urllib3.disable_warnings()
import yaml

def load_set(path):
    out = set()
    with open(path) as fh:
        for line in fh:
            item = line.strip().replace("sha256:", "")
            if item:
                out.add(item)
    return out

keep = load_set("/tmp/quay-gc-keep.txt")
cfg = yaml.safe_load(open("/conf/stack/config.yaml"))
dist = cfg.get("DISTRIBUTED_STORAGE_CONFIG") or {}
preference = cfg.get("DISTRIBUTED_STORAGE_PREFERENCE") or list(dist)
name = preference[0]
entry = dist[name]
params = entry[1] if isinstance(entry, list) and len(entry) > 1 else entry
endpoint = "https://" + params.get("host", params.get("hostname", ""))
port = str(params.get("port", "") or "")
secure = str(params.get("is_secure", "true")).lower() in ("true", "yes", "1")
if not secure:
    endpoint = "http://" + params.get("host", params.get("hostname", ""))
if port and port not in ("443", "80", ""):
    endpoint += ":" + port
bucket = params.get("s3_bucket", params.get("bucket_name", ""))
prefix = str(params.get("storage_path", "")).lstrip("/")
if prefix and not prefix.endswith("/"):
    prefix += "/"

s3 = boto3.client(
    "s3", endpoint_url=endpoint,
    aws_access_key_id=params.get("s3_access_key", params.get("access_key")),
    aws_secret_access_key=params.get("s3_secret_key", params.get("secret_key")),
    region_name="us-east-1", verify=False,
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
)
now = datetime.now(timezone.utc)
tops = {}
sha_count = sha_bytes = keep_count = keep_bytes = 0
orphan_count = orphan_bytes = 0
upload_count = upload_bytes = 0
other_count = other_bytes = 0
seen_keep = set()
upload_age = {"lt_1d": [0, 0], "d1_7": [0, 0], "gt_7d": [0, 0]}
samples = []
n = 0
token = None
while True:
    kw = {"Bucket": bucket, "MaxKeys": 1000}
    if token:
        kw["ContinuationToken"] = token
    resp = s3.list_objects_v2(**kw)
    for obj in resp.get("Contents", []):
        n += 1
        key = obj["Key"]
        size = int(obj.get("Size") or 0)
        top = key.split("/", 1)[0]
        slot = tops.setdefault(top, [0, 0])
        slot[0] += 1
        slot[1] += size
        if prefix and key.startswith(prefix):
            rel = key[len(prefix):]
        else:
            rel = None
        parts = rel.split("/") if rel is not None else []
        if len(parts) >= 3 and parts[0] == "sha256":
            digest = parts[-1]
            sha_count += 1
            sha_bytes += size
            if digest in keep:
                keep_count += 1
                keep_bytes += size
                seen_keep.add(digest)
            else:
                orphan_count += 1
                orphan_bytes += size
                if len(samples) < 5:
                    samples.append({"key": key, "size": size})
        elif parts and parts[0] == "uploads":
            upload_count += 1
            upload_bytes += size
            lm = obj.get("LastModified")
            age = (now - lm).total_seconds() if lm else 0
            bucket_name = "lt_1d" if age < 86400 else "d1_7" if age < 7 * 86400 else "gt_7d"
            upload_age[bucket_name][0] += 1
            upload_age[bucket_name][1] += size
        elif rel is not None:
            other_count += 1
            other_bytes += size
        if n % 20000 == 0:
            print(f"PROGRESS listed={n} orphan={orphan_count}", flush=True)
    if not resp.get("IsTruncated"):
        break
    token = resp.get("NextContinuationToken")

print("RESULT " + json.dumps({
    "bucket": bucket,
    "prefix": prefix.rstrip("/"),
    "endpoint_host": params.get("host", params.get("hostname", "")),
    "listed": n,
    "tops": {k: {"count": v[0], "bytes": v[1]} for k, v in sorted(tops.items())},
    "sha_count": sha_count,
    "sha_bytes": sha_bytes,
    "keep_count": keep_count,
    "keep_bytes": keep_bytes,
    "orphan_count": orphan_count,
    "orphan_bytes": orphan_bytes,
    "linked_missing": len(keep - seen_keep - {"a3ed95caeb02ffe68cdd9fd84406680ae93d633cb16422d00e8a7c22955b46d4"}),
    "upload_count": upload_count,
    "upload_bytes": upload_bytes,
    "upload_age": {k: {"count": v[0], "bytes": v[1]} for k, v in upload_age.items()},
    "other_count": other_count,
    "other_bytes": other_bytes,
    "samples": samples,
}), flush=True)
'''

DELETER = r'''
import boto3
from botocore.client import Config
import urllib3
urllib3.disable_warnings()
import yaml

def load_set(path):
    out = set()
    with open(path) as fh:
        for line in fh:
            item = line.strip().replace("sha256:", "")
            if item:
                out.add(item)
    return out

protect = load_set("/tmp/quay-gc-protect.txt")
cfg = yaml.safe_load(open("/conf/stack/config.yaml"))
dist = cfg.get("DISTRIBUTED_STORAGE_CONFIG") or {}
preference = cfg.get("DISTRIBUTED_STORAGE_PREFERENCE") or list(dist)
params = dist[preference[0]]
params = params[1] if isinstance(params, list) and len(params) > 1 else params
host = params.get("host", params.get("hostname", ""))
secure = str(params.get("is_secure", "true")).lower() in ("true", "yes", "1")
endpoint = ("https://" if secure else "http://") + host
port = str(params.get("port", "") or "")
if port and port not in ("443", "80", ""):
    endpoint += ":" + port
bucket = params.get("s3_bucket", params.get("bucket_name", ""))
prefix = str(params.get("storage_path", "")).lstrip("/")
if prefix and not prefix.endswith("/"):
    prefix += "/"

s3 = boto3.client(
    "s3", endpoint_url=endpoint,
    aws_access_key_id=params.get("s3_access_key", params.get("access_key")),
    aws_secret_access_key=params.get("s3_secret_key", params.get("secret_key")),
    region_name="us-east-1", verify=False,
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
)

token = None
listed = 0
while True:
    kw = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
    if token:
        kw["ContinuationToken"] = token
    resp = s3.list_objects_v2(**kw)
    for obj in resp.get("Contents", []):
        listed += 1
        key = obj["Key"]
        rel = key[len(prefix):] if key.startswith(prefix) else key
        parts = rel.split("/")
        if len(parts) >= 3 and parts[0] == "sha256":
            digest = parts[-1]
            if len(digest) == 64 and digest not in protect:
                print("KEY " + key, flush=True)
    if listed and listed % 20000 == 0:
        print(f"PROGRESS listed={listed}", flush=True)
    if not resp.get("IsTruncated"):
        break
    token = resp.get("NextContinuationToken")
print(f"DONE listed={listed}", flush=True)
'''


def copy_to_container(host, quay_ct, path, text):
    ssh(host, f"sudo podman exec -i {quay_ct} tee {path} >/dev/null",
        check=True, input_text=text)


def cleanup_remote(host, quay_ct):
    ssh(host,
        f"sudo podman exec {quay_ct} rm -f /tmp/quay-gc-keep.txt "
        f"/tmp/quay-gc-protect.txt /tmp/quay-gc-keys.txt",
        check=False)


def export_keep(host, pg_ct):
    linked = psql(host, pg_ct, """
        SELECT DISTINCT replace(i.content_checksum, 'sha256:', '')
        FROM imagestorage i
        WHERE i.content_checksum IS NOT NULL
          AND (
              EXISTS (SELECT 1 FROM manifestblob m WHERE m.blob_id = i.id)
              OR EXISTS (SELECT 1 FROM uploadedblob u WHERE u.blob_id = i.id)
          );
    """)
    appr = psql(host, pg_ct, """
        SELECT replace(digest, 'sha256:', '') FROM apprblob WHERE digest IS NOT NULL;
    """)
    digests = {EMPTY_LAYER}
    for blob in (linked, appr):
        for line in blob.splitlines():
            item = line.strip().replace("sha256:", "")
            if item:
                digests.add(item)
    return digests


def db_stats(host, pg_ct):
    row = psql(host, pg_ct, f"""
        SELECT
          (SELECT count(*) FROM imagestorage),
          (SELECT coalesce(sum(image_size), 0) FROM imagestorage),
          (SELECT count(*) FROM imagestorage i WHERE {ORPHAN_WHERE}),
          (SELECT coalesce(sum(i.image_size), 0) FROM imagestorage i WHERE {ORPHAN_WHERE});
    """)
    parts = row.split("|")
    return {
        "rows": int(parts[0]),
        "bytes": int(parts[1]),
        "orphan_rows": int(parts[2]),
        "orphan_bytes": int(parts[3]),
    }


def assert_known_fks(host, pg_ct):
    rows = psql(host, pg_ct, """
        SELECT conrelid::regclass::text || ' ' || pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE confrelid = 'imagestorage'::regclass
        ORDER BY 1;
    """)
    allowed = ("imagestorageplacement", "imagestoragesignature", "manifestblob", "uploadedblob")
    unexpected = []
    for line in rows.splitlines():
        table = line.split()[0] if line.strip() else ""
        if table and not any(table.startswith(name) for name in allowed):
            unexpected.append(line.strip())
    if unexpected:
        raise RuntimeError(
            "unexpected tables reference imagestorage:\n" + "\n".join(unexpected))


def delete_orphan_rows(host, pg_ct):
    """Delete unlinked rows that exist now. Do not chase rows created later."""
    out = psql(host, pg_ct, f"""
        SELECT i.id
        FROM imagestorage i
        WHERE {ORPHAN_WHERE};
    """, timeout=180)
    ids = [line.strip() for line in out.splitlines() if line.strip().isdigit()]
    if not ids:
        print("  no unlinked imagestorage rows in the snapshot", flush=True)
        return []
    deleted = []
    for offset in range(0, len(ids), 2000):
        id_list = ",".join(ids[offset:offset + 2000])
        out = psql(host, pg_ct, f"""
            WITH doomed AS (
                SELECT i.id, replace(i.content_checksum, 'sha256:', '') AS digest
                FROM imagestorage i
                WHERE i.id IN ({id_list})
                  AND {ORPHAN_WHERE}
            ),
            del_sig AS (
                DELETE FROM imagestoragesignature s
                USING doomed d WHERE s.storage_id = d.id
            ),
            del_place AS (
                DELETE FROM imagestorageplacement p
                USING doomed d WHERE p.storage_id = d.id
            )
            DELETE FROM imagestorage i
            USING doomed d
            WHERE i.id = d.id
            RETURNING d.digest;
        """, timeout=180)
        batch = [line.strip() for line in out.splitlines() if line.strip()]
        deleted.extend(batch)
        print(f"  removed {len(deleted)} / {len(ids)} snapshotted rows", flush=True)
    return deleted


def digests_still_present(host, pg_ct, digests):
    present = set()
    items = [d for d in digests if len(d) == 64 and all(c in "0123456789abcdef" for c in d)]
    for i in range(0, len(items), 400):
        chunk = items[i:i + 400]
        quoted = ",".join("'sha256:" + d + "'" for d in chunk)
        out = psql(host, pg_ct, f"""
            SELECT replace(content_checksum, 'sha256:', '')
            FROM imagestorage WHERE content_checksum IN ({quoted})
            UNION
            SELECT replace(digest, 'sha256:', '')
            FROM apprblob WHERE digest IN ({quoted});
        """)
        for line in out.splitlines():
            item = line.strip().replace("sha256:", "")
            if item:
                present.add(item)
    return present


def delete_s3_keys_file(host, quay_ct, keys):
    copy_to_container(host, quay_ct, "/tmp/quay-gc-keys.txt", "\n".join(keys) + "\n")
    script = r'''
import yaml, boto3
from botocore.client import Config
import urllib3
urllib3.disable_warnings()
keys = [line.strip() for line in open("/tmp/quay-gc-keys.txt") if line.strip()]
cfg = yaml.safe_load(open("/conf/stack/config.yaml"))
dist = cfg["DISTRIBUTED_STORAGE_CONFIG"]
preference = cfg.get("DISTRIBUTED_STORAGE_PREFERENCE") or list(dist)
params = dist[preference[0]]
params = params[1] if isinstance(params, list) else params
host = params.get("host", params.get("hostname", ""))
secure = str(params.get("is_secure", "true")).lower() in ("true", "yes", "1")
endpoint = ("https://" if secure else "http://") + host
port = str(params.get("port", "") or "")
if port and port not in ("443", "80", ""):
    endpoint += ":" + port
bucket = params.get("s3_bucket", params.get("bucket_name"))
s3 = boto3.client(
    "s3", endpoint_url=endpoint,
    aws_access_key_id=params.get("s3_access_key", params.get("access_key")),
    aws_secret_access_key=params.get("s3_secret_key", params.get("secret_key")),
    region_name="us-east-1", verify=False,
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
)
ok = fail = 0
for i in range(0, len(keys), 1000):
    chunk = keys[i:i + 1000]
    resp = s3.delete_objects(
        Bucket=bucket,
        Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
    )
    failed = resp.get("Errors") or []
    fail += len(failed)
    ok += len(chunk) - len(failed)
    print(f"PROGRESS ok={ok} fail={fail}", flush=True)
    for err in failed[:5]:
        print("ERROR " + err.get("Key", "") + " " + err.get("Message", ""), flush=True)
print(f"DONE ok={ok} fail={fail}", flush=True)
'''
    ok = fail = 0
    for line in stream_remote_python(host, quay_ct, script, timeout=900):
        if line.startswith("PROGRESS"):
            print("  " + line, flush=True)
        elif line.startswith("DONE"):
            parts = dict(p.split("=", 1) for p in line.split() if "=" in p)
            ok = int(parts.get("ok", 0))
            fail = int(parts.get("fail", 0))
        elif line.startswith("ERROR"):
            log_warn(line)
    ssh(host, f"sudo podman exec {quay_ct} rm -f /tmp/quay-gc-keys.txt", check=False)
    return ok, fail


def collect_delete_keys(host, quay_ct):
    keys = []
    for line in stream_remote_python(host, quay_ct, DELETER, timeout=900):
        if line.startswith("KEY "):
            keys.append(line[4:].strip())
        elif line.startswith("PROGRESS"):
            print("  " + line, flush=True)
    return keys


def run_scan(host, quay_ct, keep):
    copy_to_container(host, quay_ct, "/tmp/quay-gc-keep.txt", "\n".join(sorted(keep)) + "\n")
    result = None
    for line in stream_remote_python(host, quay_ct, SCANNER, timeout=900):
        if line.startswith("PROGRESS"):
            print("  " + line, flush=True)
        elif line.startswith("RESULT "):
            result = json.loads(line[len("RESULT "):])
    if not result:
        raise RuntimeError("S3 scan produced no result")
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Remove unreferenced Quay blobs from S3 on a RHEL host")
    parser.add_argument("host", help="SSH target, for example cquay3")
    parser.add_argument("--delete", action="store_true",
                        help="Delete unlinked database rows and their S3 objects")
    parser.add_argument("--force", action="store_true",
                        help="Required with --delete")
    parser.add_argument("--allow-prod", action="store_true",
                        help="Allow --delete on cquay0")
    args = parser.parse_args()

    if args.delete and not args.force:
        log_error("Refusing --delete without --force.")
        log_error("Dry-run first, then pass --delete --force.")
        sys.exit(1)
    if args.delete and args.host in PROD_HOSTS and not args.allow_prod:
        log_error(f"{args.host} is production. Pass --allow-prod to delete there.")
        sys.exit(1)

    print()
    print("=== Quay S3 garbage collection (RHEL) ===")
    print()
    log_info(f"Host: {args.host}")
    probe = ssh(args.host, "hostname -f", check=False)
    if probe.returncode != 0:
        log_error(f"Cannot SSH to {args.host}")
        if ssh.last_stderr:
            log_error(ssh.last_stderr.splitlines()[-1])
        sys.exit(1)
    log_info(f"Remote: {probe.stdout.strip()}")

    pg_ct = detect_container(args.host, "postgresql-quay|postgres.*quay", "postgres|pgsql|postgresql")
    if not pg_ct:
        log_error("Could not find a running PostgreSQL container")
        if ssh.last_stderr:
            log_error(ssh.last_stderr.splitlines()[-1])
        sys.exit(1)
    quay_ct = detect_container(args.host, "^quay |quay-app|quay-registry", "quay/quay-rhel")
    if not quay_ct:
        log_error("Could not find a running Quay container")
        sys.exit(1)
    log_info(f"PostgreSQL container: {pg_ct}")
    log_info(f"Quay container: {quay_ct}")

    try:
        assert_known_fks(args.host, pg_ct)
        stats = db_stats(args.host, pg_ct)
        if stats["rows"] == 0:
            log_error("imagestorage is empty. Refusing to continue.")
            sys.exit(1)
        keep = export_keep(args.host, pg_ct)
        if len(keep) <= 1:
            log_error("Keep-set is empty. Refusing to continue.")
            sys.exit(1)
        log_info(f"Database blobs: {stats['rows']} ({human_size(stats['bytes'])})")
        log_info(
            f"Unlinked rows: {stats['orphan_rows']} ({human_size(stats['orphan_bytes'])})")
        log_info(f"Linked digests kept: {len(keep - {EMPTY_LAYER})}")

        print()
        log_info("Listing the bucket...")
        scan = run_scan(args.host, quay_ct, keep)
        print()
        print(f"Bucket: {scan['bucket']}  prefix: {scan['prefix']}  endpoint: {scan['endpoint_host']}")
        print("Top-level prefixes:")
        for name, info in scan["tops"].items():
            print(f"  {name:<24} {info['count']:>8} objects  {human_size(info['bytes']):>12}")
        print()
        print(f"Linked blobs in S3:     {scan['keep_count']:>8}  {human_size(scan['keep_bytes'])}")
        print(f"Unlinked blobs in S3:   {scan['orphan_count']:>8}  {human_size(scan['orphan_bytes'])}")
        print(f"Linked digests missing: {scan['linked_missing']:>8}")
        print(f"uploads/:               {scan['upload_count']:>8}  {human_size(scan['upload_bytes'])}")
        age = scan["upload_age"]
        print(f"  younger than 1 day:   {age['lt_1d']['count']:>8}  {human_size(age['lt_1d']['bytes'])}")
        print(f"  1 to 7 days:          {age['d1_7']['count']:>8}  {human_size(age['d1_7']['bytes'])}")
        print(f"  older than 7 days:    {age['gt_7d']['count']:>8}  {human_size(age['gt_7d']['bytes'])}")
        if scan["samples"]:
            print()
            print("Sample unlinked objects:")
            for sample in scan["samples"]:
                print(f"  {sample['key']}  {human_size(sample['size'])}")

        if scan["linked_missing"]:
            log_warn(
                f"{scan['linked_missing']} linked digests have no S3 object. "
                "They are kept in the database.")

        if not args.delete:
            print()
            print("[DRY-RUN] No rows or objects deleted.")
            print(f"          Re-run with --delete --force to remove "
                  f"{scan['orphan_count']} unlinked S3 objects "
                  f"({human_size(scan['orphan_bytes'])}) "
                  f"and {stats['orphan_rows']} database rows.")
            return

        if scan["orphan_count"] == 0 and stats["orphan_rows"] == 0:
            log_info("Nothing to delete.")
            return

        print()
        log_info("Deleting unlinked imagestorage rows...")
        deleted_digests = delete_orphan_rows(args.host, pg_ct)
        log_info(f"Deleted {len(deleted_digests)} imagestorage rows")

        # Rebuild the keep-set after the delete, then list S3 objects that
        # are no longer referenced. A digest that was pushed again is skipped.
        log_info("Collecting S3 keys for digests that are no longer referenced...")
        copy_to_container(
            args.host, quay_ct, "/tmp/quay-gc-protect.txt",
            "\n".join(sorted(export_keep(args.host, pg_ct))) + "\n")
        keys = collect_delete_keys(args.host, quay_ct)
        print(f"  keys to delete: {len(keys)}")
        if keys:
            digest_of = []
            keyed = []
            for key in keys:
                digest = key.rsplit("/", 1)[-1]
                if digest == EMPTY_LAYER or len(digest) != 64:
                    continue
                digest_of.append(digest)
                keyed.append((digest, key))
            present = digests_still_present(args.host, pg_ct, digest_of)
            final_keys = [key for digest, key in keyed if digest not in present]
            skipped = len(keyed) - len(final_keys)
            if skipped:
                log_warn(f"Skipped {skipped} objects whose digest is referenced again")
            log_info(f"Deleting {len(final_keys)} S3 objects...")
            ok, fail = delete_s3_keys_file(args.host, quay_ct, final_keys)
            log_info(f"S3 delete ok={ok} fail={fail}")
            if fail:
                log_warn("Some deletes failed. Re-run the same command to retry.")
    finally:
        cleanup_remote(args.host, quay_ct)


if __name__ == "__main__":
    main()
