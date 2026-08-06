#!/usr/bin/env bash
# quay-s3-gc.sh v2.00
#
# Reconcile and clean up orphaned S3 objects in a Quay NooBaa bucket.
#
# Two types of orphans are handled:
#
#   A) Orphaned sha256 content blobs: When Ceph was full, Quay GC'd blobs
#      from its database but the S3 DELETE calls to NooBaa failed silently.
#
#   B) Stale upload objects: When Ceph was full, Quay blob uploads created
#      partial upload objects (datastorage/registry/uploads/*) that were
#      never finalized. These are always orphaned — Quay finalizes uploads
#      by creating sha256 objects; upload keys are purely temporary.
#
# This script:
#   1. Exports all content_checksum values from Quay's imagestorage table
#   2. Lists every S3 object key in the NooBaa quay-datastore bucket
#   3. Identifies orphaned sha256 blobs (in S3 but not in Quay DB)
#   4. Identifies stale upload objects (always orphaned)
#   5. In dry-run mode (default): reports orphan count and total size
#   6. In delete mode (--delete): removes orphaned objects in batches
#
# Usage:
#   export KUBECONFIG=/path/to/kubeconfig
#   ./quay-s3-gc.sh                  # dry-run (default)
#   ./quay-s3-gc.sh --delete         # actually delete orphans
#
# Requirements:
#   - oc (OpenShift CLI) in PATH
#   - python3 (3.7+)
#   - Quay with NooBaa-backed objectstorage
#
# License: Apache-2.0
# Maintainers: Vincent Cojot & Johann Peyrard

set -euo pipefail

DELETE_MODE=false
BATCH_SIZE=100

for arg in "$@"; do
    case "$arg" in
        --delete) DELETE_MODE=true ;;
        --help|-h)
            echo "Usage: $0 [--delete]"
            echo "  Default: dry-run mode (report orphans only)"
            echo "  --delete: actually delete orphaned S3 objects"
            exit 0
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

if [[ -z "${KUBECONFIG:-}" ]]; then
    echo "(EE) KUBECONFIG is not set."
    echo "    export KUBECONFIG=/path/to/kubeconfig"
    exit 1
fi

HUB_API=$(oc whoami --show-server 2>/dev/null || true)
if [[ -z "$HUB_API" ]]; then
    echo "(EE) Cannot reach cluster via KUBECONFIG=$KUBECONFIG"
    exit 1
fi
HUB_SHORT=$(echo "$HUB_API" | sed 's|https\?://api\.||; s|:[0-9]*/?$||' | cut -d. -f1)
echo "(II) Connected to: $HUB_SHORT ($HUB_API)"

# Discover Quay namespace
QUAY_NS=""
for ns in quay quay-enterprise; do
    if oc get namespace "$ns" &>/dev/null; then
        QUAY_NS="$ns"
        break
    fi
done
if [[ -z "$QUAY_NS" ]]; then
    echo "(EE) No Quay namespace found."
    exit 1
fi
echo "(II) Quay namespace: $QUAY_NS"

# Discover Quay DB pod and database name
QUAY_DB_POD=""
for label in "quay-component=postgres" "quay-component=quay-database"; do
    QUAY_DB_POD=$(oc get pods -n "$QUAY_NS" -l "$label" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true
    [[ -n "$QUAY_DB_POD" ]] && break
done
if [[ -z "$QUAY_DB_POD" ]]; then
    echo "(EE) No Quay database pod found in $QUAY_NS."
    exit 1
fi

QUAY_CR=$(oc get quayregistry -n "$QUAY_NS" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true
QUAY_DB="${QUAY_CR:-regional-quay}-quay-database"
echo "(II) Quay DB pod: $QUAY_DB_POD  DB: $QUAY_DB"

# Discover NooBaa
NOOBAA_POD=$(oc get pods -n openshift-storage -l noobaa-core=noobaa \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true
if [[ -z "$NOOBAA_POD" ]]; then
    echo "(EE) No NooBaa core pod found."
    exit 1
fi

# Verify NooBaa pod is Running and Ready
NOOBAA_PHASE=$(oc get pod "$NOOBAA_POD" -n openshift-storage \
    -o jsonpath='{.status.phase}' 2>/dev/null) || true
NOOBAA_READY=$(oc get pod "$NOOBAA_POD" -n openshift-storage \
    -o jsonpath='{.status.containerStatuses[?(@.name=="core")].ready}' 2>/dev/null) || true
if [[ "$NOOBAA_PHASE" != "Running" || "$NOOBAA_READY" != "true" ]]; then
    echo "(EE) NooBaa core pod $NOOBAA_POD is not ready (phase=$NOOBAA_PHASE, ready=$NOOBAA_READY)."
    echo "    Wait for NooBaa to be healthy before running this script."
    exit 1
fi

# Find the Quay OBC and its bucket name
QUAY_OBC=$(oc get obc -n "$QUAY_NS" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true
if [[ -z "$QUAY_OBC" ]]; then
    echo "(EE) No ObjectBucketClaim found in $QUAY_NS."
    exit 1
fi
BUCKET_NAME=$(oc get configmap "$QUAY_OBC" -n "$QUAY_NS" \
    -o jsonpath='{.data.BUCKET_NAME}' 2>/dev/null) || true
if [[ -z "$BUCKET_NAME" ]]; then
    echo "(EE) Cannot determine bucket name from OBC $QUAY_OBC."
    exit 1
fi
echo "(II) NooBaa bucket: $BUCKET_NAME"

# Get NooBaa admin token
NB_PASS=$(oc get secret noobaa-admin -n openshift-storage \
    -o jsonpath='{.data.password}' | base64 -d)

echo ""

# ---------------------------------------------------------------------------
# Step 1: Get all referenced checksums from Quay DB
# ---------------------------------------------------------------------------

echo "[1/5] Exporting Quay DB content checksums..."
TMPDIR=$(mktemp -d /tmp/quay-s3-gc.XXXXXX)
trap 'rm -rf "$TMPDIR"' EXIT

QUAY_HASHES="${TMPDIR}/quay-hashes"
S3KEYS="${TMPDIR}/s3keys"
S3UPLOADS="${TMPDIR}/s3uploads"
ORPHANS="${TMPDIR}/orphans"

oc exec "$QUAY_DB_POD" -n "$QUAY_NS" -- \
    psql -U postgres -d "$QUAY_DB" -P pager=off -t -A -c \
    "SELECT content_checksum FROM imagestorage
     WHERE content_checksum IS NOT NULL;" \
    | sed 's/^sha256://' | sort -u > "$QUAY_HASHES"

QUAY_COUNT=$(wc -l < "$QUAY_HASHES")
echo "  Quay DB has $QUAY_COUNT unique content checksums."

# ---------------------------------------------------------------------------
# Step 2: List all S3 objects in the NooBaa quay bucket
# ---------------------------------------------------------------------------

echo "[2/5] Listing all S3 objects in NooBaa bucket (this may take a while)..."

S3RAW="${TMPDIR}/s3raw"
oc exec -n openshift-storage "$NOOBAA_POD" -c core -- python3 -c "
import json, urllib.request, sys, os

def rpc(api, method, params=None, token=None):
    body = {'api': api, 'method': method, 'params': params or {}}
    if token:
        body['auth_token'] = token
    req = urllib.request.Request('http://localhost:8080/rpc/',
        data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req).read())

auth = rpc('auth_api', 'create_auth', {
    'role': 'admin', 'system': 'noobaa',
    'email': 'admin@noobaa.io', 'password': '$NB_PASS'
})
token = auth['reply']['token']

key_marker = None
total = 0
sha256_count = 0
upload_count = 0
while True:
    params = {'bucket': '$BUCKET_NAME', 'limit': 1000}
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
            # sha256 content blob: hash\tsize
            print(f'SHA256\t{parts[4]}\t{size}')
            sha256_count += 1
        elif len(parts) >= 4 and parts[2] == 'uploads':
            # Stale upload: full_key\tsize
            print(f'UPLOAD\t{key}\t{size}')
            upload_count += 1
        total += 1
    key_marker = objects[-1]['key']
    if total % 10000 == 0:
        print(f'PROGRESS\t{total}', flush=True)
    if not result['reply'].get('is_truncated', False):
        break

print(f'SUMMARY\t{total}\t{sha256_count}\t{upload_count}')
" > "$S3RAW"

# Parse the raw output into separate files
grep '^SHA256' "$S3RAW" | cut -f2,3 > "$S3KEYS" || true
grep '^UPLOAD' "$S3RAW" | cut -f2,3 > "$S3UPLOADS" || true
grep '^PROGRESS' "$S3RAW" | while IFS=$'\t' read _ n; do
    echo "  ... listed $n objects so far"
done || true

S3_SHA_COUNT=$(wc -l < "$S3KEYS")
S3_UPLOAD_COUNT=$(wc -l < "$S3UPLOADS")
S3_TOTAL=$((S3_SHA_COUNT + S3_UPLOAD_COUNT))
echo "  NooBaa bucket has $S3_TOTAL S3 objects total:"
echo "    - $S3_SHA_COUNT sha256 content blobs"
echo "    - $S3_UPLOAD_COUNT stale upload objects"

# Calculate stale upload size
UPLOAD_SIZE=0
if [[ $S3_UPLOAD_COUNT -gt 0 ]]; then
    UPLOAD_SIZE=$(awk -F'\t' '{s+=$2} END {print s+0}' "$S3UPLOADS")
fi
UPLOAD_SIZE_HR=$(python3 -c "
b=$UPLOAD_SIZE
if b >= 1024**4: print(f'{b/1024**4:.2f} TiB')
elif b >= 1024**3: print(f'{b/1024**3:.2f} GiB')
elif b >= 1024**2: print(f'{b/1024**2:.0f} MiB')
else: print(f'{b} B')
")
if [[ $S3_UPLOAD_COUNT -gt 0 ]]; then
    echo "    - Stale upload size: $UPLOAD_SIZE_HR"
fi

# ---------------------------------------------------------------------------
# Step 3: Find orphaned sha256 blobs (in S3 but not in Quay DB)
# ---------------------------------------------------------------------------

echo "[3/5] Computing orphaned sha256 blobs..."

# S3KEYS format: hash\tsize
# QUAY_HASHES format: hash (sorted)
cut -f1 "$S3KEYS" | sort -u > "${S3KEYS}.sorted_hashes"
comm -23 "${S3KEYS}.sorted_hashes" "$QUAY_HASHES" > "${ORPHANS}.hashes"

ORPHAN_COUNT=$(wc -l < "${ORPHANS}.hashes")

ORPHAN_SIZE=0
if [[ $ORPHAN_COUNT -gt 0 ]]; then
    ORPHAN_SIZE=$(grep -Ff "${ORPHANS}.hashes" "$S3KEYS" \
        | awk -F'\t' '{s+=$2} END {print s+0}')
fi

ORPHAN_SIZE_HR=$(python3 -c "
b=$ORPHAN_SIZE
if b >= 1024**4: print(f'{b/1024**4:.2f} TiB')
elif b >= 1024**3: print(f'{b/1024**3:.2f} GiB')
elif b >= 1024**2: print(f'{b/1024**2:.0f} MiB')
else: print(f'{b} B')
")

# Combined totals
TOTAL_ORPHAN_COUNT=$((ORPHAN_COUNT + S3_UPLOAD_COUNT))
TOTAL_ORPHAN_SIZE=$((ORPHAN_SIZE + UPLOAD_SIZE))
TOTAL_ORPHAN_SIZE_HR=$(python3 -c "
b=$TOTAL_ORPHAN_SIZE
if b >= 1024**4: print(f'{b/1024**4:.2f} TiB')
elif b >= 1024**3: print(f'{b/1024**3:.2f} GiB')
elif b >= 1024**2: print(f'{b/1024**2:.0f} MiB')
else: print(f'{b} B')
")

echo ""
echo "  ============================================================"
echo "  Quay DB blobs:                $QUAY_COUNT"
echo "  NooBaa S3 sha256 objects:     $S3_SHA_COUNT"
echo "  NooBaa S3 upload objects:     $S3_UPLOAD_COUNT"
echo "  ---"
echo "  Orphaned sha256 blobs:        $ORPHAN_COUNT ($ORPHAN_SIZE_HR)"
echo "  Stale upload objects:         $S3_UPLOAD_COUNT ($UPLOAD_SIZE_HR)"
echo "  ---"
echo "  TOTAL orphaned objects:       $TOTAL_ORPHAN_COUNT ($TOTAL_ORPHAN_SIZE_HR logical)"
echo "  TOTAL raw waste (3x repl.):   ~$(python3 -c "print(f'{$TOTAL_ORPHAN_SIZE*3/1024**4:.2f}')") TiB"
echo "  ============================================================"
echo ""

if [[ $TOTAL_ORPHAN_COUNT -eq 0 ]]; then
    echo "(II) No orphaned objects found. Bucket is clean."
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 4: Dry-run report
# ---------------------------------------------------------------------------

if [[ "$DELETE_MODE" != "true" ]]; then
    echo "[4/5] DRY-RUN mode — no objects deleted."
    echo "  Re-run with --delete to remove $TOTAL_ORPHAN_COUNT orphaned objects."
    echo ""
    if [[ $ORPHAN_COUNT -gt 0 ]]; then
        echo "  Sample orphaned sha256 blobs (first 10):"
        head -10 "${ORPHANS}.hashes" | while read h; do
            sz=$(grep "^${h}" "$S3KEYS" | cut -f2)
            echo "    sha256:${h}  size=$sz"
        done
        echo ""
    fi
    if [[ $S3_UPLOAD_COUNT -gt 0 ]]; then
        echo "  Sample stale upload objects (first 10):"
        head -10 "$S3UPLOADS" | while IFS=$'\t' read key sz; do
            echo "    ${key}  size=$sz"
        done
        echo ""
    fi
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 5: Delete orphans
# ---------------------------------------------------------------------------

echo "[5/5] DELETE mode — removing $TOTAL_ORPHAN_COUNT orphaned S3 objects in batches of $BATCH_SIZE..."
echo "  This will free ~$TOTAL_ORPHAN_SIZE_HR of logical storage (~$(python3 -c "print(f'{$TOTAL_ORPHAN_SIZE*3/1024**4:.2f}')") TiB raw)."
echo ""

# Build combined delete list with full S3 keys
DELETE_KEYS="${TMPDIR}/delete-keys"

# Add orphaned sha256 blobs (reconstruct full S3 keys)
while IFS= read -r hash; do
    prefix="${hash:0:2}"
    echo "datastorage/registry/sha256/${prefix}/${hash}"
done < "${ORPHANS}.hashes" > "$DELETE_KEYS"

# Add stale upload objects (already have full keys)
if [[ $S3_UPLOAD_COUNT -gt 0 ]]; then
    cut -f1 "$S3UPLOADS" >> "$DELETE_KEYS"
fi

DELETED=0
FAILED=0
TOTAL_TO_DELETE=$(wc -l < "$DELETE_KEYS")

echo "  Deleting $ORPHAN_COUNT orphaned sha256 blobs + $S3_UPLOAD_COUNT stale uploads..."
echo ""

# Process in batches — read BATCH_SIZE lines at a time
while true; do
    BATCH_LINES=()
    for (( i=0; i<BATCH_SIZE; i++ )); do
        IFS= read -r line || break
        [[ -n "$line" ]] && BATCH_LINES+=("$line")
    done
    [[ ${#BATCH_LINES[@]} -eq 0 ]] && break

    BATCH_JSON=$(printf '%s\n' "${BATCH_LINES[@]}" | python3 -c "
import sys, json
keys = [line.strip() for line in sys.stdin if line.strip()]
print(json.dumps(keys))
")

    RESULT=$(oc exec -n openshift-storage "$NOOBAA_POD" -c core -- python3 -c "
import json, urllib.request

def rpc(api, method, params=None, token=None):
    body = {'api': api, 'method': method, 'params': params or {}}
    if token:
        body['auth_token'] = token
    req = urllib.request.Request('http://localhost:8080/rpc/',
        data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req).read())

auth = rpc('auth_api', 'create_auth', {
    'role': 'admin', 'system': 'noobaa',
    'email': 'admin@noobaa.io', 'password': '$NB_PASS'
})
token = auth['reply']['token']

keys = $BATCH_JSON
ok = 0
fail = 0
for key in keys:
    try:
        rpc('object_api', 'delete_object', {
            'bucket': '$BUCKET_NAME',
            'key': key,
        }, token)
        ok += 1
    except Exception as e:
        print(f'WARN: Failed to delete {key}: {e}')
        fail += 1

print(f'BATCH_RESULT ok={ok} fail={fail}')
" 2>&1)

    batch_ok=$(echo "$RESULT" | grep 'BATCH_RESULT' | sed 's/.*ok=\([0-9]*\).*/\1/')
    batch_fail=$(echo "$RESULT" | grep 'BATCH_RESULT' | sed 's/.*fail=\([0-9]*\).*/\1/')
    DELETED=$((DELETED + ${batch_ok:-0}))
    FAILED=$((FAILED + ${batch_fail:-0}))
    echo "  Progress: $DELETED / $TOTAL_TO_DELETE deleted ($FAILED failures)"

done < "$DELETE_KEYS"

echo ""
echo "  =========================================="
echo "  Deleted:  $DELETED objects"
echo "  Failed:   $FAILED objects"
echo "  =========================================="
echo ""

if [[ $FAILED -gt 0 ]]; then
    echo "(WW) Some deletions failed. Re-run the script to retry."
fi

echo "(II) Done. Run odf-storage-report.py to verify storage reclamation."
echo "(II) Note: Ceph may take some time to reflect the freed space."
