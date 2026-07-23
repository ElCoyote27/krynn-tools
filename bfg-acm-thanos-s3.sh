#!/bin/bash
# Fix ACM Observability when Thanos S3 storage is full.
#
# What it does:
#   1. Scales down all Thanos/observability writers to stop ingestion
#   2. Purges the existing bucket (drops all metrics)
#   3. Recreates a clean empty bucket
#   4. Patches the thanos-object-storage secret with valid credentials
#   5. Restarts all observability pods cleanly
#
# WARNING: This DESTROYS all historical Thanos metrics.
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

# --- Step 1: Check Ceph health ---
echo ""
echo "=== [1/6] Checking Ceph cluster health ==="
oc exec -n "$NS_STOR" "$TOOLS" -- ceph status 2>&1 | head -20 || true
echo ""
oc exec -n "$NS_STOR" "$TOOLS" -- ceph df 2>&1 | head -15 || true
echo ""

# --- Step 2: Scale down writers to stop filling storage ---
echo "=== [2/6] Scaling down Thanos writers ==="
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
echo "=== [3/6] Fetching RGW credentials ==="

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
echo "=== [4/6] Purging existing bucket ==="

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

# --- Step 5: Delete observability PVCs (free block storage) ---
echo ""
echo "=== [5/7] Deleting observability PVCs ==="
echo "  Current PVCs:"
oc get pvc -n "$NS_OBS" --no-headers 2>/dev/null | awk '{printf "    %-60s %s\n", $1, $4}' || true
echo ""
echo "  Deleting all PVCs in $NS_OBS..."
oc delete pvc --all -n "$NS_OBS" --wait=false 2>/dev/null || true
echo "  PVCs marked for deletion (Ceph will reclaim space shortly)."

# --- Step 6: Create fresh bucket and patch secret ---
echo ""
echo "=== [6/7] Creating fresh bucket and patching secret ==="

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

# --- Step 7: Restart all observability components ---
echo ""
echo "=== [7/7] Restarting observability stack ==="

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

echo ""
echo "============================================="
echo " DONE. All Thanos metrics have been purged."
echo " New bucket: $NEW_BUCKET"
echo " Pods are restarting — check with:"
echo "   oc get pods -n $NS_OBS"
echo "   oc exec -n $NS_STOR $TOOLS -- ceph df"
echo "============================================="
