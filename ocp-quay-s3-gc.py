#!/usr/bin/env python3
"""
ocp-quay-s3-gc.py — Reconcile and clean up orphaned S3 objects in a
Quay NooBaa bucket on OpenShift.

Two types of orphans are handled:

  A) Orphaned sha256 content blobs: When Ceph was full, Quay GC'd blobs
     from its database but the S3 DELETE calls to NooBaa failed silently.

  B) Stale upload objects: When Ceph was full, Quay blob uploads created
     partial upload objects (datastorage/registry/uploads/*) that were
     never finalized.  These are always orphaned.

Usage:
  export KUBECONFIG=/path/to/kubeconfig
  python3 ocp-quay-s3-gc.py                  # dry-run (default)
  python3 ocp-quay-s3-gc.py --delete         # actually delete orphans

Requirements:
  - oc (OpenShift CLI) in PATH
  - python3 (3.7+)
  - Quay with NooBaa-backed objectstorage

License: Apache-2.0
Maintainers: Vincent Cojot & Johann Peyrard
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

RED    = "\033[0;31m"
GREEN  = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN   = "\033[0;36m"
NC     = "\033[0m"

def log_info(msg):  print(f"(II) {msg}", flush=True)
def log_warn(msg):  print(f"(WW) {msg}", flush=True)
def log_error(msg): print(f"(EE) {msg}", flush=True)


def run(cmd, check=True, timeout=300):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed ({r.returncode}): {cmd}\n{r.stderr}")
    return r.stdout.strip()


def oc(args, check=True):
    return run(f"oc {args}", check=check)


def oc_exec(ns, pod, cmd, check=True, timeout=600, container=None):
    c_flag = f"-c {container}" if container else ""
    full = f"oc exec -n {ns} {pod} {c_flag} -- bash -c {repr(cmd)}"
    return run(full, check=check, timeout=timeout)


def human_size(b):
    b = int(b)
    if b >= 1024**4: return f"{b/1024**4:.2f} TiB"
    if b >= 1024**3: return f"{b/1024**3:.2f} GiB"
    if b >= 1024**2: return f"{b/1024**2:.0f} MiB"
    return f"{b} B"


def main():
    parser = argparse.ArgumentParser(
        description="Reconcile and clean up orphaned S3 objects in a Quay NooBaa bucket")
    parser.add_argument("--delete", action="store_true",
                        help="Actually delete orphaned objects (default: dry-run)")
    parser.add_argument("--namespace", default="",
                        help="Quay namespace (auto-detected)")
    parser.add_argument("--batch-size", type=int, default=100,
                        help="Delete batch size (default: 100)")
    args = parser.parse_args()

    # ─── Pre-flight ─────────────────────────────────────────────────
    hub_api = oc("whoami --show-server", check=False)
    if not hub_api:
        log_error("Cannot reach cluster. Check $KUBECONFIG or ~/.kube/config.")
        sys.exit(1)
    hub_short = hub_api.replace("https://", "").replace("http://", "").split(":")[0]
    hub_short = hub_short.replace("api.", "").split(".")[0]
    log_info(f"Connected to: {hub_short} ({hub_api})")

    # ─── Discover Quay namespace ────────────────────────────────────
    quay_ns = args.namespace
    if not quay_ns:
        quay_ns = oc("get quayregistry -A -o jsonpath='{.items[0].metadata.namespace}'",
                     check=False)
    if not quay_ns:
        quay_ns = oc("get pods -A -l quay-component=quay-app "
                     "-o jsonpath='{.items[0].metadata.namespace}'", check=False)
    if not quay_ns:
        out = oc("get pods -A", check=False)
        for line in out.splitlines():
            if ("quay" in line.lower() and ("app" in line.lower() or "database" in line.lower())
                    and "clair" not in line.lower()):
                quay_ns = line.split()[0]; break
    if not quay_ns:
        log_error("No Quay namespace found. Use --namespace.")
        sys.exit(1)
    log_info(f"Quay namespace: {quay_ns}")

    # ─── Discover DB pod ────────────────────────────────────────────
    pg_pod = ""
    for label in ("quay-component=postgres", "quay-component=quay-database"):
        pg_pod = oc(f"get pods -n {quay_ns} -l {label} "
                    f"-o jsonpath='{{.items[0].metadata.name}}'", check=False)
        if pg_pod:
            break
    if not pg_pod:
        out = oc(f"get pods -n {quay_ns} -o name", check=False)
        for line in out.splitlines():
            if ("database" in line.lower() or "postgres" in line.lower()) \
                    and "clair" not in line.lower():
                pg_pod = line.replace("pod/", "").strip(); break
    if not pg_pod:
        log_error(f"No Quay database pod found in {quay_ns}.")
        sys.exit(1)

    # Auto-detect DB user and name
    db_user_out = oc_exec(quay_ns, pg_pod,
        "psql -U postgres -t -c \"SELECT usename FROM pg_user WHERE usename != 'postgres';\"",
        check=False)
    db_user = ""
    for line in db_user_out.splitlines():
        if line.strip():
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
    log_info(f"Quay DB pod: {pg_pod}  DB: {db_name} (user: {db_user})")

    # ─── Discover NooBaa ────────────────────────────────────────────
    noobaa_pod = oc("get pods -n openshift-storage -l noobaa-core=noobaa "
                    "-o jsonpath='{.items[0].metadata.name}'", check=False)
    if not noobaa_pod:
        log_error("No NooBaa core pod found.")
        sys.exit(1)

    nb_phase = oc(f"get pod {noobaa_pod} -n openshift-storage "
                  f"-o jsonpath='{{.status.phase}}'", check=False)
    if nb_phase != "Running":
        log_error(f"NooBaa core pod {noobaa_pod} is not running (phase={nb_phase}).")
        sys.exit(1)

    # Find OBC and bucket
    quay_obc = oc(f"get obc -n {quay_ns} -o jsonpath='{{.items[0].metadata.name}}'",
                  check=False)
    if not quay_obc:
        log_error(f"No ObjectBucketClaim found in {quay_ns}.")
        sys.exit(1)
    bucket_name = oc(f"get configmap {quay_obc} -n {quay_ns} "
                     f"-o jsonpath='{{.data.BUCKET_NAME}}'", check=False)
    if not bucket_name:
        log_error(f"Cannot determine bucket name from OBC {quay_obc}.")
        sys.exit(1)
    log_info(f"NooBaa bucket: {bucket_name}")

    # Get NooBaa admin password
    import base64
    nb_pass_b64 = oc("get secret noobaa-admin -n openshift-storage "
                     "-o jsonpath='{.data.password}'", check=False)
    nb_pass = base64.b64decode(nb_pass_b64).decode("utf-8")

    print()

    # ─── Step 1: Export DB checksums ────────────────────────────────
    print("[1/5] Exporting Quay DB content checksums...")
    hashes_out = oc_exec(quay_ns, pg_pod,
        f"psql -U {db_user} -d {db_name} -P pager=off -t -A -c "
        f"\"SELECT content_checksum FROM imagestorage WHERE content_checksum IS NOT NULL;\"",
        check=False, timeout=120)
    quay_hashes = set()
    for line in hashes_out.splitlines():
        h = line.strip().replace("sha256:", "")
        if h:
            quay_hashes.add(h)
    print(f"  Quay DB has {len(quay_hashes)} unique content checksums.")

    # ─── Step 2: List S3 objects via NooBaa RPC ─────────────────────
    print("[2/5] Listing all S3 objects in NooBaa bucket (this may take a while)...")

    # Build the listing script — credentials passed via JSON file
    list_script = r'''
import json, urllib.request, sys

def rpc(api, method, params=None, token=None):
    body = {'api': api, 'method': method, 'params': params or {}}
    if token:
        body['auth_token'] = token
    req = urllib.request.Request('http://localhost:8080/rpc/',
        data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req).read())

cfg = json.load(open('/tmp/_nb_gc_creds.json'))
auth = rpc('auth_api', 'create_auth', {
    'role': 'admin', 'system': 'noobaa',
    'email': 'admin@noobaa.io', 'password': cfg['password']
})
token = auth['reply']['token']
bucket = cfg['bucket']

key_marker = None
total = 0
while True:
    params = {'bucket': bucket, 'limit': 1000}
    if key_marker:
        params['key_marker'] = key_marker
    result = rpc('object_api', 'list_objects', params, token)
    objects = result['reply'].get('objects', [])
    if not objects:
        break
    for obj in objects:
        key = obj.get('key', '')
        size = obj.get('size', 0)
        parts = key.split('/')
        if len(parts) >= 5 and parts[2] == 'sha256':
            print(f'SHA256\t{parts[4]}\t{size}')
        elif len(parts) >= 4 and parts[2] == 'uploads':
            print(f'UPLOAD\t{key}\t{size}')
        total += 1
    key_marker = objects[-1]['key']
    if total % 10000 == 0:
        print(f'PROGRESS\t{total}', flush=True)
    if not result['reply'].get('is_truncated', False):
        break
print(f'DONE\t{total}')
'''

    # Write creds and script to temp files, oc cp them
    creds_data = {"password": nb_pass, "bucket": bucket_name}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as cf:
        json.dump(creds_data, cf)
        creds_path = cf.name
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as sf:
        sf.write(list_script)
        script_path = sf.name

    try:
        run(f"oc cp {creds_path} openshift-storage/{noobaa_pod}:/tmp/_nb_gc_creds.json -c core")
        run(f"oc cp {script_path} openshift-storage/{noobaa_pod}:/tmp/_nb_gc_list.py -c core")
    finally:
        os.unlink(creds_path)
        os.unlink(script_path)

    raw = oc_exec("openshift-storage", noobaa_pod,
                  "python3 /tmp/_nb_gc_list.py",
                  check=False, timeout=3600, container="core")

    sha256_objects = {}   # hash → size
    upload_objects = {}   # key → size
    for line in raw.splitlines():
        parts = line.split("\t")
        if parts[0] == "SHA256" and len(parts) >= 3:
            sha256_objects[parts[1]] = int(parts[2])
        elif parts[0] == "UPLOAD" and len(parts) >= 3:
            upload_objects[parts[1]] = int(parts[2])
        elif parts[0] == "PROGRESS":
            print(f"  ... listed {parts[1]} objects so far", flush=True)

    s3_sha_count = len(sha256_objects)
    s3_upload_count = len(upload_objects)
    upload_size = sum(upload_objects.values())

    print(f"  NooBaa bucket has {s3_sha_count + s3_upload_count} S3 objects total:")
    print(f"    - {s3_sha_count} sha256 content blobs")
    print(f"    - {s3_upload_count} stale upload objects")
    if s3_upload_count > 0:
        print(f"    - Stale upload size: {human_size(upload_size)}")

    # ─── Step 3: Find orphans ───────────────────────────────────────
    print("[3/5] Computing orphaned sha256 blobs...")
    orphan_hashes = set(sha256_objects.keys()) - quay_hashes
    orphan_size = sum(sha256_objects[h] for h in orphan_hashes)

    total_orphan_count = len(orphan_hashes) + s3_upload_count
    total_orphan_size = orphan_size + upload_size

    print()
    print("  ============================================================")
    print(f"  Quay DB blobs:                {len(quay_hashes)}")
    print(f"  NooBaa S3 sha256 objects:     {s3_sha_count}")
    print(f"  NooBaa S3 upload objects:     {s3_upload_count}")
    print(f"  ---")
    print(f"  Orphaned sha256 blobs:        {len(orphan_hashes)} ({human_size(orphan_size)})")
    print(f"  Stale upload objects:         {s3_upload_count} ({human_size(upload_size)})")
    print(f"  ---")
    print(f"  TOTAL orphaned objects:       {total_orphan_count} ({human_size(total_orphan_size)} logical)")
    print(f"  TOTAL raw waste (3x repl.):   ~{total_orphan_size*3/1024**4:.2f} TiB")
    print("  ============================================================")
    print()

    if total_orphan_count == 0:
        log_info("No orphaned objects found. Bucket is clean.")
        sys.exit(0)

    # ─── Step 4: Dry-run or delete ──────────────────────────────────
    if not args.delete:
        print("[4/5] DRY-RUN mode — no objects deleted.")
        print(f"  Re-run with --delete to remove {total_orphan_count} orphaned objects.")
        print()
        if orphan_hashes:
            print("  Sample orphaned sha256 blobs (first 10):")
            for h in sorted(orphan_hashes)[:10]:
                print(f"    sha256:{h}  size={sha256_objects[h]}")
            print()
        if upload_objects:
            print("  Sample stale upload objects (first 10):")
            for key in sorted(upload_objects.keys())[:10]:
                print(f"    {key}  size={upload_objects[key]}")
            print()
        sys.exit(0)

    # ─── Step 5: Delete orphans ─────────────────────────────────────
    print(f"[5/5] DELETE mode — removing {total_orphan_count} orphaned objects "
          f"in batches of {args.batch_size}...")
    print(f"  This will free ~{human_size(total_orphan_size)} of logical storage "
          f"(~{total_orphan_size*3/1024**4:.2f} TiB raw).")
    print()

    # Build full S3 key list
    delete_keys = []
    for h in orphan_hashes:
        delete_keys.append(f"datastorage/registry/sha256/{h[:2]}/{h}")
    for key in upload_objects:
        delete_keys.append(key)

    # Delete script — reads keys from JSON file
    delete_script = r'''
import json, urllib.request, sys

def rpc(api, method, params=None, token=None):
    body = {'api': api, 'method': method, 'params': params or {}}
    if token:
        body['auth_token'] = token
    req = urllib.request.Request('http://localhost:8080/rpc/',
        data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req).read())

cfg = json.load(open('/tmp/_nb_gc_creds.json'))
auth = rpc('auth_api', 'create_auth', {
    'role': 'admin', 'system': 'noobaa',
    'email': 'admin@noobaa.io', 'password': cfg['password']
})
token = auth['reply']['token']
bucket = cfg['bucket']
keys = json.load(open('/tmp/_nb_gc_keys.json'))

ok = fail = 0
for key in keys:
    try:
        rpc('object_api', 'delete_object', {'bucket': bucket, 'key': key}, token)
        ok += 1
    except Exception as e:
        print(f'WARN: Failed to delete {key}: {e}', flush=True)
        fail += 1
    if (ok + fail) % 500 == 0:
        print(f'PROGRESS {ok + fail} ok={ok} fail={fail}', flush=True)
print(f'BATCH_RESULT ok={ok} fail={fail}')
'''

    total_deleted = 0
    total_failed = 0

    for i in range(0, len(delete_keys), args.batch_size):
        batch = delete_keys[i:i + args.batch_size]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as kf:
            json.dump(batch, kf)
            keys_path = kf.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as df:
            df.write(delete_script)
            del_script_path = df.name

        try:
            run(f"oc cp {keys_path} openshift-storage/{noobaa_pod}:/tmp/_nb_gc_keys.json -c core")
            run(f"oc cp {del_script_path} openshift-storage/{noobaa_pod}:/tmp/_nb_gc_del.py -c core")
        finally:
            os.unlink(keys_path)
            os.unlink(del_script_path)

        result = oc_exec("openshift-storage", noobaa_pod,
                         "python3 /tmp/_nb_gc_del.py",
                         check=False, timeout=600, container="core")

        for line in result.splitlines():
            if line.startswith("BATCH_RESULT"):
                parts = line.split()
                for p in parts:
                    if p.startswith("ok="):
                        total_deleted += int(p.split("=")[1])
                    elif p.startswith("fail="):
                        total_failed += int(p.split("=")[1])

        print(f"  Progress: {total_deleted} / {len(delete_keys)} deleted "
              f"({total_failed} failures)", flush=True)

    print()
    print("  ==========================================")
    print(f"  Deleted:  {total_deleted} objects")
    print(f"  Failed:   {total_failed} objects")
    print("  ==========================================")
    print()

    if total_failed > 0:
        log_warn("Some deletions failed. Re-run the script to retry.")

    log_info("Done. Run odf-storage-report.py to verify storage reclamation.")
    log_info("Note: Ceph may take some time to reflect the freed space.")


if __name__ == "__main__":
    main()
