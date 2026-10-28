#!/bin/bash
# Fix ACM Observability when Thanos S3 storage is full.
#
# What it does:
#   1. Checks Ceph health, raises full ratios if cluster is full, boosts recovery speed
#   2. Scales down all Thanos/observability writers to stop ingestion
#   3. Fetches RGW credentials (OBC or prometheus-user)
#   4. Purges the existing RGW bucket (drops all metrics) + RGW garbage collection
#   5. Checks NooBaa DB for orphaned dedup blocks from deleted NooBaa buckets
#   6. Deletes observability PVCs to free block storage
#   7. Recreates a clean empty bucket + patches thanos-object-storage secret
#   8. Restarts all observability pods cleanly, resets Ceph recovery speed
#
# WARNING: This DESTROYS all historical Thanos metrics.
#
# NOTE: For Quay NooBaa bucket cleanup (orphaned sha256 blobs and stale
# upload objects from Ceph-full incidents), use quay-s3-gc.sh instead.
#
# Usage: KUBECONFIG=/path/to/kubeconfig ./bfg-acm-thanos-s3.sh
set -euo pipefail

if [ -z "${KUBECONFIG:-}" ]; then
  echo "ERROR: KUBECONFIG is not set."
  echo "Usage: KUBECONFIG=/path/to/kubeconfig ./bfg-acm-thanos-s3.sh"
  exit 1
fi

if [ ! -f "$KUBECONFIG" ]; then
  echo "ERROR: KUBECONFIG file not found: $KUBECONFIG"
  exit 1
fi

NS_OBS="open-cluster-management-observability"
NS_STOR="openshift-storage"
REALM="ocs-storagecluster-cephobjectstore"
RGW_HOST="rook-ceph-rgw-${REALM}.${NS_STOR}.svc"
TOOLS="deploy/rook-ceph-tools"
OBC_NAME="grafana-mgmt"

echo "============================================="
echo " ACM Thanos S3 Storage Reset (DESTRUCTIVE)"
echo "============================================="
echo ""

# --- Step 0: Confirm ---
read -p "This will DELETE all Thanos metrics and recreate the bucket. Continue? [y/N] " CONFIRM
[[ "$CONFIRM" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# --- Step 1: Check Ceph health and unblock if full ---
echo ""
echo "=== [1/8] Checking Ceph cluster health ==="
oc exec -n "$NS_STOR" "$TOOLS" -- ceph status 2>&1 | head -20 || true
echo ""
oc exec -n "$NS_STOR" "$TOOLS" -- ceph df 2>&1 | head -15 || true
echo ""

# If any OSDs are full/backfillfull, temporarily raise ratios to unblock I/O
CEPH_HEALTH=$(oc exec -n "$NS_STOR" "$TOOLS" -- ceph health 2>/dev/null || true)
if echo "$CEPH_HEALTH" | grep -qiE "full osd|pool.*full"; then
  echo "  WARNING: Ceph cluster has full OSDs/pools — raising ratios temporarily"
  echo "  Saving current ratios..."
  ORIG_FULL=$(oc exec -n "$NS_STOR" "$TOOLS" -- ceph osd dump 2>/dev/null | awk '/^full_ratio/{print $2}' || true)
  ORIG_BACKFILL=$(oc exec -n "$NS_STOR" "$TOOLS" -- ceph osd dump 2>/dev/null | awk '/^backfillfull_ratio/{print $2}' || true)
  ORIG_NEAR=$(oc exec -n "$NS_STOR" "$TOOLS" -- ceph osd dump 2>/dev/null | awk '/^nearfull_ratio/{print $2}' || true)
  echo "  Current: full=$ORIG_FULL backfillfull=$ORIG_BACKFILL nearfull=$ORIG_NEAR"
  oc exec -n "$NS_STOR" "$TOOLS" -- ceph osd set-full-ratio 0.95 2>&1 || true
  oc exec -n "$NS_STOR" "$TOOLS" -- ceph osd set-backfillfull-ratio 0.92 2>&1 || true
  oc exec -n "$NS_STOR" "$TOOLS" -- ceph osd set-nearfull-ratio 0.90 2>&1 || true
  echo "  Ratios raised to 0.95/0.92/0.90 — I/O should be unblocked."
  echo ""
fi

# Speed up Ceph recovery for the duration of this cleanup
echo "  Boosting Ceph recovery speed (temporary)..."
oc exec -n "$NS_STOR" "$TOOLS" -- ceph config set osd osd_mclock_override_recovery_settings true 2>&1 || true
oc exec -n "$NS_STOR" "$TOOLS" -- ceph config set osd osd_recovery_max_active 16 2>&1 || true
oc exec -n "$NS_STOR" "$TOOLS" -- ceph config set osd osd_max_backfills 8 2>&1 || true
CEPH_RECOVERY_BOOSTED=true
echo ""

# --- Step 2: Scale down writers to stop filling storage ---
echo "=== [2/8] Scaling down Thanos writers ==="
# Stop thanos-receive and thanos-compact to prevent new writes
for COMP in receive compact rule store-gateway; do
  DEPLOY=$(oc get deploy,statefulset -n "$NS_OBS" -o name 2>/dev/null | grep -i "thanos.*${COMP}" || true)
  for D in $DEPLOY; do
    echo "  Scaling down $D"
    oc scale "$D" -n "$NS_OBS" --replicas=0 2>/dev/null || true
  done
done
# Also scale observatorium-operator managed components
for STS in $(oc get statefulset -n "$NS_OBS" -o name 2>/dev/null | grep -E "thanos|observ" || true); do
  echo "  Scaling down $STS"
  oc scale "$STS" -n "$NS_OBS" --replicas=0 2>/dev/null || true
done
echo "  Waiting 10s for pods to terminate..."
sleep 10

# --- Step 3: Get credentials (prometheus-user or OBC user) ---
echo ""
echo "=== [3/8] Fetching RGW credentials ==="

# Try to get the OBC-generated user first
OBC_SECRET=$(oc get obc "$OBC_NAME" -n "$NS_OBS" -o jsonpath='{.spec.secretName}' 2>/dev/null || true)
if [ -n "$OBC_SECRET" ]; then
  AK=$(oc get secret "$OBC_SECRET" -n "$NS_OBS" -o jsonpath='{.data.AWS_ACCESS_KEY_ID}' 2>/dev/null | base64 -d || true)
  SK=$(oc get secret "$OBC_SECRET" -n "$NS_OBS" -o jsonpath='{.data.AWS_SECRET_ACCESS_KEY}' 2>/dev/null | base64 -d || true)
fi

# Fall back to prometheus-user via radosgw-admin
if [ -z "${AK:-}" ] || [ -z "${SK:-}" ]; then
  echo "  OBC secret not available, trying prometheus-user..."

  # Detect available realms/zonegroups in case REALM is wrong
  echo "  Checking RGW realms..."
  oc exec -n "$NS_STOR" "$TOOLS" -- radosgw-admin realm list 2>&1 || true

  # Try with configured realm
  RGW_JSON=$(oc exec -n "$NS_STOR" "$TOOLS" -- \
    radosgw-admin user info --uid=prometheus-user --rgw-realm="$REALM" \
    --format=json 2>&1) || true

  # If that failed, try without --rgw-realm (single-realm clusters don't need it)
  if [ -z "$RGW_JSON" ] || ! echo "$RGW_JSON" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    echo "  Failed with --rgw-realm=$REALM, trying without realm flag..."
    echo "  (error was: ${RGW_JSON:-<empty>})"
    RGW_JSON=$(oc exec -n "$NS_STOR" "$TOOLS" -- \
      radosgw-admin user info --uid=prometheus-user --format=json 2>&1) || true
  fi

  # If user doesn't exist, create it
  if [ -z "$RGW_JSON" ] || ! echo "$RGW_JSON" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    echo "  prometheus-user not found, creating it..."
    echo "  (error was: ${RGW_JSON:-<empty>})"
    RGW_JSON=$(oc exec -n "$NS_STOR" "$TOOLS" -- \
      radosgw-admin user create --uid=prometheus-user --display-name="Prometheus Thanos" \
      --rgw-realm="$REALM" --format=json 2>&1) || true
    # Try without realm too
    if [ -z "$RGW_JSON" ] || ! echo "$RGW_JSON" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
      echo "  Create with realm failed, trying without realm..."
      echo "  (error was: ${RGW_JSON:-<empty>})"
      RGW_JSON=$(oc exec -n "$NS_STOR" "$TOOLS" -- \
        radosgw-admin user create --uid=prometheus-user --display-name="Prometheus Thanos" \
        --format=json 2>&1) || true
    fi
  fi

  if [ -n "$RGW_JSON" ] && echo "$RGW_JSON" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    read -r AK SK < <(echo "$RGW_JSON" | python3 -c "
import sys,json; k=json.load(sys.stdin)['keys'][0]
print(k['access_key'], k['secret_key'])")
  else
    echo ""
    echo "  All radosgw-admin attempts failed."
    echo "  Last output: ${RGW_JSON:-<empty>}"
    echo ""
    echo "  Diagnosing RGW status..."
    echo "  --- RGW pods ---"
    oc get pods -n "$NS_STOR" -l app=rook-ceph-rgw 2>&1 || true
    echo "  --- Ceph RGW services ---"
    oc exec -n "$NS_STOR" "$TOOLS" -- ceph service dump 2>&1 | grep -A5 rgw || true
  fi
fi

if [ -z "${AK:-}" ] || [ -z "${SK:-}" ]; then
  echo ""
  echo "ERROR: Cannot obtain any S3 credentials."
  echo ""
  echo "  Possible causes:"
  echo "    - RGW pod is not running (check: oc get pods -n $NS_STOR -l app=rook-ceph-rgw)"
  echo "    - Realm '$REALM' does not exist"
  echo "    - rook-ceph-tools pod cannot reach RGW"
  echo ""
  echo "  Manual workaround: create the user directly"
  echo "    oc exec -n $NS_STOR $TOOLS -- radosgw-admin user create \\"
  echo "      --uid=prometheus-user --display-name='Prometheus Thanos'"
  exit 1
fi
echo "  Access Key: $AK"

# --- Step 4: Identify and purge the bucket ---
echo ""
echo "=== [4/8] Purging existing bucket ==="

BUCKET=$(oc get secret thanos-object-storage -n "$NS_OBS" \
  -o jsonpath='{.data.thanos\.yaml}' 2>/dev/null | base64 -d | awk '/bucket:/{print $2}' || true)

# Also check OBC configmap for bucket name
if [ -z "$BUCKET" ]; then
  OBC_CM=$(oc get obc "$OBC_NAME" -n "$NS_OBS" -o jsonpath='{.spec.configMapName}' 2>/dev/null || true)
  [ -n "$OBC_CM" ] && BUCKET=$(oc get cm "$OBC_CM" -n "$NS_OBS" -o jsonpath='{.data.BUCKET_NAME}' 2>/dev/null || true)
fi

if [ -n "$BUCKET" ]; then
  echo "  Purging bucket: $BUCKET"
  # Remove all objects then the bucket itself
  oc exec -n "$NS_STOR" "$TOOLS" -- \
    radosgw-admin bucket rm --bucket="$BUCKET" --purge-objects --rgw-realm="$REALM" 2>&1 || true
  echo "  Bucket purged."
else
  echo "  No existing bucket found to purge."
fi

# Also check for any other grafana-mgmt-* buckets and purge them
echo "  Checking for orphaned grafana-mgmt buckets..."
BUCKET_LIST=$(oc exec -n "$NS_STOR" "$TOOLS" -- \
  radosgw-admin bucket list --rgw-realm="$REALM" 2>/dev/null || true)
for ORPHAN in $(echo "$BUCKET_LIST" | python3 -c "
import sys,json
try:
  buckets=json.load(sys.stdin)
  for b in buckets:
    name = b if isinstance(b,str) else b.get('bucket','')
    if name.startswith('grafana-mgmt'):
      print(name)
except: pass
" 2>/dev/null); do
  echo "  Purging orphan bucket: $ORPHAN"
  oc exec -n "$NS_STOR" "$TOOLS" -- \
    radosgw-admin bucket rm --bucket="$ORPHAN" --purge-objects --rgw-realm="$REALM" 2>&1 || true
done

# Force RGW garbage collection (actual RADOS object deletion is deferred)
echo "  Forcing RGW garbage collection..."
oc exec -n "$NS_STOR" "$TOOLS" -- radosgw-admin gc process --rgw-realm="$REALM" 2>&1 || true
oc exec -n "$NS_STOR" "$TOOLS" -- radosgw-admin gc process 2>&1 || true
echo "  GC processed."

# --- Step 4b: Verify no orphaned NooBaa blocks remain ---
# When Thanos was stored via NooBaa (OBC with openshift-storage.noobaa.io),
# the RGW purge above only cleans the direct RGW bucket. NooBaa dedup blocks
# live in a separate backing bucket. Query the NooBaa DB to check for orphans.
echo ""
echo "=== [5/8] Checking NooBaa for orphaned blocks ==="

NOOBAA_CORE=$(oc get pods -n "$NS_STOR" -l noobaa-core=noobaa \
  -o name 2>/dev/null | head -1)
NOOBAA_DB=$(oc get pods -n "$NS_STOR" -l noobaa-db=noobaa \
  -o name 2>/dev/null | head -1)

if [ -n "$NOOBAA_DB" ]; then
  # Check which NooBaa buckets own blocks, and whether any are orphaned
  echo "  Querying NooBaa DB for block ownership..."
  BLOCK_OWNERS=$(oc exec -n "$NS_STOR" "$NOOBAA_DB" -- \
    psql -U noobaa -d nbcore -t -c "
      SELECT b.data->>'bucket' AS bucket_id,
             COALESCE(bkt.data->>'name', '<DELETED>') AS bucket_name,
             COUNT(*) AS blocks,
             pg_size_pretty(SUM((b.data->>'size')::bigint)) AS total_size
      FROM datablocks b
      LEFT JOIN buckets bkt ON bkt.data->>'_id' = b.data->>'bucket'
      GROUP BY b.data->>'bucket', bkt.data->>'name'
      ORDER BY blocks DESC;
    " 2>/dev/null || true)
  echo "$BLOCK_OWNERS" | while read -r line; do
    [ -n "$line" ] && echo "    $line"
  done

  # Check for deleted-but-not-reclaimed blocks (pending GC)
  PENDING_GC=$(oc exec -n "$NS_STOR" "$NOOBAA_DB" -- \
    psql -U noobaa -d nbcore -t -c "
      SELECT COUNT(*) AS blocks,
             pg_size_pretty(COALESCE(SUM((data->>'size')::bigint),0)) AS size
      FROM datablocks
      WHERE data ? 'deleted' AND NOT data ? 'reclaimed';
    " 2>/dev/null || true)
  echo "  Deleted blocks pending reclaim: $PENDING_GC"

  # If any blocks point to a <DELETED> bucket, they are true orphans.
  # The NooBaa blocks_reclaimer should clean these, but if Ceph was full
  # it may have been blocked. Restart NooBaa core to trigger a fresh sweep.
  if echo "$BLOCK_OWNERS" | grep -q '<DELETED>'; then
    echo ""
    echo "  WARNING: Found orphaned blocks belonging to deleted NooBaa buckets!"
    echo "  Restarting NooBaa core to trigger blocks reclaimer sweep..."
    oc delete pod -n "$NS_STOR" -l noobaa-core=noobaa --wait=false 2>/dev/null || true
    echo "  Monitor with: oc logs -n $NS_STOR -l noobaa-core=noobaa -c core -f | grep -i reclaim"
  else
    echo "  No orphaned NooBaa blocks found — all blocks belong to active buckets."
  fi
else
  echo "  NooBaa DB pod not found, skipping orphan check."
fi

# --- Step 6: Delete observability PVCs (free block storage) ---
echo ""
echo "=== [6/8] Deleting observability PVCs ==="
echo "  Current PVCs:"
oc get pvc -n "$NS_OBS" --no-headers 2>/dev/null | awk '{printf "    %-60s %s\n", $1, $4}' || true
echo ""
echo "  Deleting all PVCs in $NS_OBS..."
oc delete pvc --all -n "$NS_OBS" --wait=false 2>/dev/null || true
echo "  PVCs marked for deletion (Ceph will reclaim space shortly)."

# --- Step 7: Create fresh bucket and patch secret ---
echo ""
echo "=== [7/8] Creating fresh bucket and patching secret ==="

NEW_BUCKET="${OBC_NAME}-$(date +%Y%m%d-%H%M%S)"
echo "  New bucket: $NEW_BUCKET"

# Create bucket via S3 API (radosgw-admin has no 'bucket create' in rhceph-8)
echo "  Creating bucket via S3 API..."
oc exec -n "$NS_STOR" "$TOOLS" -- python3 -c "
import http.client,ssl,hashlib,hmac,datetime,base64
now=datetime.datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S +0000')
sig=base64.b64encode(hmac.new('${SK}'.encode(),
  ('PUT\n\n\n'+now+'\n/${NEW_BUCKET}/').encode(),hashlib.sha1).digest()).decode()
ctx=ssl.create_default_context();ctx.check_hostname=False;ctx.verify_mode=ssl.CERT_NONE
c=http.client.HTTPSConnection('${RGW_HOST}',443,context=ctx)
c.request('PUT','/${NEW_BUCKET}/',headers={'Host':'${RGW_HOST}','Date':now,
  'Authorization':'AWS ${AK}:'+sig})
r=c.getresponse();print(f'{r.status} {r.reason}')
assert r.status in (200,409),'Bucket creation failed: '+r.reason
"

# Link bucket to user so credentials have access
oc exec -n "$NS_STOR" "$TOOLS" -- \
  radosgw-admin bucket link --bucket="$NEW_BUCKET" --uid=prometheus-user \
  --rgw-realm="$REALM" 2>&1 || true

echo "  Patching thanos-object-storage secret..."
CONFIG=$(cat <<EOF | base64 -w0
type: s3
config:
  bucket: $NEW_BUCKET
  endpoint: ${RGW_HOST}:443
  http_config:
    insecure_skip_verify: true
    tls_config:
      insecure_skip_verify: true
  insecure: false
  access_key: $AK
  secret_key: $SK
EOF
)

# Create or patch the secret
if oc get secret thanos-object-storage -n "$NS_OBS" &>/dev/null; then
  oc patch secret thanos-object-storage -n "$NS_OBS" \
    -p "{\"data\":{\"thanos.yaml\":\"$CONFIG\"}}"
else
  oc create secret generic thanos-object-storage -n "$NS_OBS" \
    --from-literal=thanos.yaml="$(echo "$CONFIG" | base64 -d)"
fi
echo "  Secret updated."

# --- Step 8: Restart all observability components ---
echo ""
echo "=== [8/8] Restarting observability stack ==="

# Delete the OBC and recreate so the operator reconciles
oc delete obc "$OBC_NAME" -n "$NS_OBS" --ignore-not-found 2>/dev/null || true

# Scale everything back up by restarting the MCO
echo "  Restarting multicluster-observability-operator..."
oc delete pod -n "$NS_OBS" -l name=multicluster-observability-operator --wait=false 2>/dev/null || true

# Restart all thanos/observability pods
echo "  Deleting all Thanos pods for a clean restart..."
oc delete pod -n "$NS_OBS" --all --wait=false 2>/dev/null || true

echo ""
echo "=== Verifying Ceph space freed ==="
sleep 5
oc exec -n "$NS_STOR" "$TOOLS" -- ceph df 2>&1 | head -10 || true

# Reset Ceph recovery speed to defaults
if [ "${CEPH_RECOVERY_BOOSTED:-}" = "true" ]; then
  echo ""
  echo "=== Resetting Ceph recovery speed to defaults ==="
  oc exec -n "$NS_STOR" "$TOOLS" -- ceph config rm osd osd_mclock_override_recovery_settings 2>&1 || true
  oc exec -n "$NS_STOR" "$TOOLS" -- ceph config rm osd osd_recovery_max_active 2>&1 || true
  oc exec -n "$NS_STOR" "$TOOLS" -- ceph config rm osd osd_max_backfills 2>&1 || true
  echo "  Recovery speed reset to defaults."
fi

# Archive any crash reports left behind
oc exec -n "$NS_STOR" "$TOOLS" -- ceph crash archive-all 2>/dev/null || true

echo ""
echo "============================================="
echo " DONE. All Thanos metrics have been purged."
echo " New bucket: $NEW_BUCKET"
echo " Pods are restarting — check with:"
echo "   oc get pods -n $NS_OBS"
echo "   oc exec -n $NS_STOR $TOOLS -- ceph df"
if [ -n "${ORIG_FULL:-}" ]; then
echo ""
echo " NOTE: Ceph full ratios were raised to 0.95/0.92/0.90."
echo " ODF operator may reconcile them. Original values were:"
echo "   full=$ORIG_FULL backfillfull=$ORIG_BACKFILL nearfull=$ORIG_NEAR"
fi
echo "============================================="
