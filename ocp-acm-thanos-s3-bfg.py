#!/usr/bin/env python3
"""
Fix ACM Observability when Thanos S3 storage is full.

What it does:
  1. Checks Ceph health, raises full ratios if cluster is full, boosts recovery
  2. Scales down all Thanos/observability writers to stop ingestion
  3. Fetches RGW credentials (OBC or prometheus-user)
  4. Purges the existing RGW bucket (drops all metrics) + RGW garbage collection
  5. Checks NooBaa DB for orphaned dedup blocks from deleted NooBaa buckets
  6. Deletes observability PVCs to free block storage
  7. Recreates a clean empty bucket + patches thanos-object-storage secret
  8. Restarts all observability pods cleanly, resets Ceph recovery speed

WARNING: This DESTROYS all historical Thanos metrics.

NOTE: For Quay NooBaa bucket cleanup (orphaned sha256 blobs and stale
upload objects from Ceph-full incidents), use ocp-quay-s3-gc.py instead.

Usage:
  export KUBECONFIG=/path/to/kubeconfig
  python3 bfg-acm-thanos-s3.py

License: Apache-2.0
Maintainers: Vincent Cojot & Johann Peyrard
"""

import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime

NS_OBS  = "open-cluster-management-observability"
NS_STOR = "openshift-storage"
REALM   = "ocs-storagecluster-cephobjectstore"
RGW_HOST = f"rook-ceph-rgw-{REALM}.{NS_STOR}.svc"
TOOLS   = "deploy/rook-ceph-tools"
OBC_NAME = "grafana-mgmt"


def run(cmd, check=True, timeout=300):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed ({r.returncode}): {cmd}\n{r.stderr}")
    return r.stdout.strip()


def oc(args, check=True, timeout=120):
    return run(f"oc {args}", check=check, timeout=timeout)


def oc_exec(ns, resource, cmd, check=True, timeout=120):
    """oc exec into a pod or deploy/sts resource."""
    return run(f"oc exec -n {ns} {resource} -- {cmd}", check=check, timeout=timeout)


def ceph(cmd, check=False):
    """Run a ceph command via rook-ceph-tools."""
    return oc_exec(NS_STOR, TOOLS, cmd, check=check)


def rgw_admin(cmd, check=False):
    """Run radosgw-admin via rook-ceph-tools."""
    return ceph(f"radosgw-admin {cmd}", check=check)


def main():
    if not os.environ.get("KUBECONFIG"):
        print("ERROR: KUBECONFIG is not set.")
        print("Usage: KUBECONFIG=/path/to/kubeconfig python3 bfg-acm-thanos-s3.py")
        sys.exit(1)

    if not os.path.isfile(os.environ["KUBECONFIG"]):
        print(f"ERROR: KUBECONFIG file not found: {os.environ['KUBECONFIG']}")
        sys.exit(1)

    print("=============================================")
    print(" ACM Thanos S3 Storage Reset (DESTRUCTIVE)")
    print("=============================================")
    print()

    # --- Step 0: Confirm ---
    confirm = input("This will DELETE all Thanos metrics and recreate the bucket. Continue? [y/N] ")
    if confirm.lower() != "y":
        print("Aborted.")
        sys.exit(0)

    ceph_recovery_boosted = False
    orig_full = orig_backfill = orig_near = ""

    # --- Step 1: Check Ceph health ---
    print()
    print("=== [1/8] Checking Ceph cluster health ===")
    print(ceph("ceph status") or "(no output)")
    print()
    print(ceph("ceph df") or "(no output)")
    print()

    ceph_health = ceph("ceph health")
    if ceph_health and ("full osd" in ceph_health.lower() or "pool" in ceph_health.lower()
                        and "full" in ceph_health.lower()):
        print("  WARNING: Ceph cluster has full OSDs/pools — raising ratios temporarily")
        print("  Saving current ratios...")
        dump = ceph("ceph osd dump")
        for line in (dump or "").splitlines():
            if "full_ratio" in line and "backfill" not in line and "near" not in line:
                orig_full = line.split()[-1] if line.split() else ""
            elif "backfillfull_ratio" in line:
                orig_backfill = line.split()[-1] if line.split() else ""
            elif "nearfull_ratio" in line:
                orig_near = line.split()[-1] if line.split() else ""
        print(f"  Current: full={orig_full} backfillfull={orig_backfill} nearfull={orig_near}")
        ceph("ceph osd set-full-ratio 0.95")
        ceph("ceph osd set-backfillfull-ratio 0.92")
        ceph("ceph osd set-nearfull-ratio 0.90")
        print("  Ratios raised to 0.95/0.92/0.90 — I/O should be unblocked.")
        print()

    print("  Boosting Ceph recovery speed (temporary)...")
    ceph("ceph config set osd osd_mclock_override_recovery_settings true")
    ceph("ceph config set osd osd_recovery_max_active 16")
    ceph("ceph config set osd osd_max_backfills 8")
    ceph_recovery_boosted = True
    print()

    # --- Step 2: Scale down writers ---
    print("=== [2/8] Scaling down Thanos writers ===")
    resources = oc(f"get deploy,statefulset -n {NS_OBS} -o name", check=False)
    for res in (resources or "").splitlines():
        if any(comp in res.lower() for comp in ("receive", "compact", "rule",
                                                 "store-gateway", "thanos", "observ")):
            print(f"  Scaling down {res}")
            oc(f"scale {res} -n {NS_OBS} --replicas=0", check=False)
    print("  Waiting 10s for pods to terminate...")
    time.sleep(10)

    # --- Step 3: Get credentials ---
    print()
    print("=== [3/8] Fetching RGW credentials ===")

    ak = sk = ""

    # Try OBC secret first
    obc_secret = oc(f"get obc {OBC_NAME} -n {NS_OBS} "
                    f"-o jsonpath='{{.spec.secretName}}'", check=False)
    if obc_secret:
        ak_b64 = oc(f"get secret {obc_secret} -n {NS_OBS} "
                    f"-o jsonpath='{{.data.AWS_ACCESS_KEY_ID}}'", check=False)
        sk_b64 = oc(f"get secret {obc_secret} -n {NS_OBS} "
                    f"-o jsonpath='{{.data.AWS_SECRET_ACCESS_KEY}}'", check=False)
        if ak_b64:
            ak = base64.b64decode(ak_b64).decode()
        if sk_b64:
            sk = base64.b64decode(sk_b64).decode()

    # Fallback: prometheus-user via radosgw-admin
    if not ak or not sk:
        print("  OBC secret not available, trying prometheus-user...")
        rgw_json = None

        # Try with realm
        raw = rgw_admin(f"user info --uid=prometheus-user --rgw-realm={REALM} --format=json")
        try:
            rgw_json = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass

        # Try without realm
        if not rgw_json:
            print(f"  Failed with --rgw-realm={REALM}, trying without realm flag...")
            raw = rgw_admin("user info --uid=prometheus-user --format=json")
            try:
                rgw_json = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass

        # Create user if needed
        if not rgw_json:
            print("  prometheus-user not found, creating it...")
            raw = rgw_admin(f"user create --uid=prometheus-user "
                            f"--display-name='Prometheus Thanos' "
                            f"--rgw-realm={REALM} --format=json")
            try:
                rgw_json = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                raw = rgw_admin("user create --uid=prometheus-user "
                                "--display-name='Prometheus Thanos' --format=json")
                try:
                    rgw_json = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    pass

        if rgw_json and "keys" in rgw_json:
            ak = rgw_json["keys"][0]["access_key"]
            sk = rgw_json["keys"][0]["secret_key"]

    if not ak or not sk:
        print()
        print("ERROR: Cannot obtain any S3 credentials.")
        print(f"  Check: oc get pods -n {NS_STOR} -l app=rook-ceph-rgw")
        sys.exit(1)

    print(f"  Access Key: {ak}")

    # --- Step 4: Purge bucket ---
    print()
    print("=== [4/8] Purging existing bucket ===")

    bucket = ""
    thanos_yaml_b64 = oc(f"get secret thanos-object-storage -n {NS_OBS} "
                         f"-o jsonpath='{{.data.thanos\\.yaml}}'", check=False)
    if thanos_yaml_b64:
        thanos_yaml = base64.b64decode(thanos_yaml_b64).decode()
        for line in thanos_yaml.splitlines():
            if "bucket:" in line:
                bucket = line.split(":", 1)[1].strip()
                break

    if not bucket:
        obc_cm = oc(f"get obc {OBC_NAME} -n {NS_OBS} "
                    f"-o jsonpath='{{.spec.configMapName}}'", check=False)
        if obc_cm:
            bucket = oc(f"get cm {obc_cm} -n {NS_OBS} "
                        f"-o jsonpath='{{.data.BUCKET_NAME}}'", check=False)

    if bucket:
        print(f"  Purging bucket: {bucket}")
        rgw_admin(f"bucket rm --bucket={bucket} --purge-objects --rgw-realm={REALM}")
        print("  Bucket purged.")
    else:
        print("  No existing bucket found to purge.")

    # Purge any orphaned grafana-mgmt buckets
    print("  Checking for orphaned grafana-mgmt buckets...")
    bucket_list_raw = rgw_admin(f"bucket list --rgw-realm={REALM}")
    try:
        bucket_list = json.loads(bucket_list_raw)
        for b in bucket_list:
            name = b if isinstance(b, str) else b.get("bucket", "")
            if name.startswith("grafana-mgmt"):
                print(f"  Purging orphan bucket: {name}")
                rgw_admin(f"bucket rm --bucket={name} --purge-objects --rgw-realm={REALM}")
    except (json.JSONDecodeError, TypeError):
        pass

    print("  Forcing RGW garbage collection...")
    rgw_admin(f"gc process --rgw-realm={REALM}")
    rgw_admin("gc process")
    print("  GC processed.")

    # --- Step 5: Check NooBaa orphans ---
    print()
    print("=== [5/8] Checking NooBaa for orphaned blocks ===")

    noobaa_db = oc(f"get pods -n {NS_STOR} -l noobaa-db=noobaa -o name", check=False)
    if noobaa_db:
        noobaa_db = noobaa_db.splitlines()[0] if noobaa_db else ""

    if noobaa_db:
        print("  Querying NooBaa DB for block ownership...")
        block_owners = oc_exec(NS_STOR, noobaa_db,
            'psql -U noobaa -d nbcore -t -c "'
            "SELECT b.data->>'bucket' AS bucket_id, "
            "COALESCE(bkt.data->>'name', '<DELETED>') AS bucket_name, "
            "COUNT(*) AS blocks, "
            "pg_size_pretty(SUM((b.data->>'size')::bigint)) AS total_size "
            "FROM datablocks b "
            "LEFT JOIN buckets bkt ON bkt.data->>'_id' = b.data->>'bucket' "
            "GROUP BY b.data->>'bucket', bkt.data->>'name' "
            'ORDER BY blocks DESC;"',
            check=False)
        if block_owners:
            for line in block_owners.splitlines():
                if line.strip():
                    print(f"    {line.strip()}")

        pending = oc_exec(NS_STOR, noobaa_db,
            'psql -U noobaa -d nbcore -t -c "'
            "SELECT COUNT(*) AS blocks, "
            "pg_size_pretty(COALESCE(SUM((data->>'size')::bigint),0)) AS size "
            "FROM datablocks "
            "WHERE data ? 'deleted' AND NOT data ? 'reclaimed';"
            '"', check=False)
        print(f"  Deleted blocks pending reclaim: {pending}")

        if block_owners and "<DELETED>" in block_owners:
            print()
            print("  WARNING: Found orphaned blocks belonging to deleted NooBaa buckets!")
            print("  Restarting NooBaa core to trigger blocks reclaimer sweep...")
            oc(f"delete pod -n {NS_STOR} -l noobaa-core=noobaa --wait=false", check=False)
        else:
            print("  No orphaned NooBaa blocks found.")
    else:
        print("  NooBaa DB pod not found, skipping orphan check.")

    # --- Step 6: Delete PVCs ---
    print()
    print("=== [6/8] Deleting observability PVCs ===")
    print("  Current PVCs:")
    pvcs = oc(f"get pvc -n {NS_OBS} --no-headers", check=False)
    if pvcs:
        for line in pvcs.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                print(f"    {parts[0]:<60s} {parts[3]}")
    print()
    print(f"  Deleting all PVCs in {NS_OBS}...")
    oc(f"delete pvc --all -n {NS_OBS} --wait=false", check=False)
    print("  PVCs marked for deletion.")

    # --- Step 7: Create fresh bucket ---
    print()
    print("=== [7/8] Creating fresh bucket and patching secret ===")

    new_bucket = f"{OBC_NAME}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
    print(f"  New bucket: {new_bucket}")

    # Create bucket via S3 API using a python script inside rook-ceph-tools
    # Credentials are passed via a JSON file to avoid escaping issues
    import tempfile

    bucket_create_script = r'''
import http.client, ssl, hashlib, hmac, datetime, base64, json, sys
cfg = json.load(open('/tmp/_bfg_creds.json'))
now = datetime.datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S +0000')
string_to_sign = f"PUT\n\n\n{now}\n/{cfg['bucket']}/"
sig = base64.b64encode(
    hmac.new(cfg['sk'].encode(), string_to_sign.encode(), hashlib.sha1).digest()
).decode()
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
c = http.client.HTTPSConnection(cfg['host'], 443, context=ctx)
c.request('PUT', f"/{cfg['bucket']}/", headers={
    'Host': cfg['host'], 'Date': now,
    'Authorization': f"AWS {cfg['ak']}:{sig}"
})
r = c.getresponse()
print(f'{r.status} {r.reason}')
assert r.status in (200, 409), f"Bucket creation failed: {r.reason}"
'''

    creds_data = {"ak": ak, "sk": sk, "bucket": new_bucket, "host": RGW_HOST}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as cf:
        json.dump(creds_data, cf)
        creds_path = cf.name
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as sf:
        sf.write(bucket_create_script)
        script_path = sf.name

    try:
        run(f"oc cp {creds_path} {NS_STOR}/{TOOLS.replace('deploy/', '')}:/tmp/_bfg_creds.json")
        # For deploy/ we need to get the actual pod name
        tools_pod = oc(f"get pods -n {NS_STOR} -l app=rook-ceph-tools "
                       f"-o jsonpath='{{.items[0].metadata.name}}'", check=False)
        if tools_pod:
            run(f"oc cp {creds_path} {NS_STOR}/{tools_pod}:/tmp/_bfg_creds.json")
            run(f"oc cp {script_path} {NS_STOR}/{tools_pod}:/tmp/_bfg_create_bucket.py")
        print("  Creating bucket via S3 API...")
        result = ceph("python3 /tmp/_bfg_create_bucket.py")
        print(f"  {result}")
    finally:
        os.unlink(creds_path)
        os.unlink(script_path)

    # Link bucket to user
    rgw_admin(f"bucket link --bucket={new_bucket} --uid=prometheus-user --rgw-realm={REALM}")

    # Build thanos config
    print("  Patching thanos-object-storage secret...")
    thanos_config = (
        f"type: s3\n"
        f"config:\n"
        f"  bucket: {new_bucket}\n"
        f"  endpoint: {RGW_HOST}:443\n"
        f"  http_config:\n"
        f"    insecure_skip_verify: true\n"
        f"    tls_config:\n"
        f"      insecure_skip_verify: true\n"
        f"  insecure: false\n"
        f"  access_key: {ak}\n"
        f"  secret_key: {sk}\n"
    )
    config_b64 = base64.b64encode(thanos_config.encode()).decode()

    exists = oc(f"get secret thanos-object-storage -n {NS_OBS}", check=False)
    if exists:
        oc(f'patch secret thanos-object-storage -n {NS_OBS} '
           f'-p \'{{"data":{{"thanos.yaml":"{config_b64}"}}}}\'')
    else:
        # Write config to temp file to avoid shell escaping
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tf:
            tf.write(thanos_config)
            config_path = tf.name
        try:
            oc(f"create secret generic thanos-object-storage -n {NS_OBS} "
               f"--from-file=thanos.yaml={config_path}")
        finally:
            os.unlink(config_path)
    print("  Secret updated.")

    # --- Step 8: Restart ---
    print()
    print("=== [8/8] Restarting observability stack ===")
    oc(f"delete obc {OBC_NAME} -n {NS_OBS} --ignore-not-found", check=False)

    print("  Restarting multicluster-observability-operator...")
    oc(f"delete pod -n {NS_OBS} -l name=multicluster-observability-operator --wait=false",
       check=False)

    print("  Deleting all Thanos pods for a clean restart...")
    oc(f"delete pod -n {NS_OBS} --all --wait=false", check=False)

    print()
    print("=== Verifying Ceph space freed ===")
    time.sleep(5)
    print(ceph("ceph df") or "(waiting for Ceph...)")

    # Reset Ceph recovery speed
    if ceph_recovery_boosted:
        print()
        print("=== Resetting Ceph recovery speed to defaults ===")
        ceph("ceph config rm osd osd_mclock_override_recovery_settings")
        ceph("ceph config rm osd osd_recovery_max_active")
        ceph("ceph config rm osd osd_max_backfills")
        print("  Recovery speed reset to defaults.")

    ceph("ceph crash archive-all")

    print()
    print("=============================================")
    print(" DONE. All Thanos metrics have been purged.")
    print(f" New bucket: {new_bucket}")
    print(" Pods are restarting — check with:")
    print(f"   oc get pods -n {NS_OBS}")
    print(f"   oc exec -n {NS_STOR} {TOOLS} -- ceph df")
    if orig_full:
        print()
        print(" NOTE: Ceph full ratios were raised to 0.95/0.92/0.90.")
        print(" ODF operator may reconcile them. Original values were:")
        print(f"   full={orig_full} backfillfull={orig_backfill} nearfull={orig_near}")
    print("=============================================")


if __name__ == "__main__":
    main()
