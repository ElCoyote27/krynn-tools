#!/bin/bash
# Flush organization repositories from OCP-based Quay (operator deployment)
# Usage: ./ocp-flush-quay.sh [--org <name>] [--namespace <ns>] [--execute] [--s3-cleanup]
#
# Requirements:
#   - oc CLI with access to the cluster ($KUBECONFIG or ~/.kube/config)
#   - For --s3-cleanup: aws CLI (locally or available inside a debug pod)

set -e

# ─── Defaults ────────────────────────────────────────────────────────
ORG_NAME="${ORG_NAME:-ocp4}"
QUAY_NS="${QUAY_NAMESPACE:-}"
LIST_ORGS=false
EXECUTE=false
S3_CLEANUP=false

# ─── Parse arguments ────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --org)         ORG_NAME="$2"; shift 2 ;;
        --org=*)       ORG_NAME="${1#*=}"; shift ;;
        --namespace)   QUAY_NS="$2"; shift 2 ;;
        --namespace=*) QUAY_NS="${1#*=}"; shift ;;
        --list-orgs)   LIST_ORGS=true; shift ;;
        --execute)     EXECUTE=true; shift ;;
        --s3-cleanup)  S3_CLEANUP=true; shift ;;
        -h|--help)     usage; exit 0 ;;
        *)             echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

usage() {
    cat << 'EOF'
Usage: ocp-flush-quay.sh [options]

Flush organization repositories from OCP-based Quay (operator deployment).
Kubeconfig, namespace, database are all auto-detected.

Options:
  --org <name>         Organization to flush (default: ocp4)
  --namespace <name>   Quay namespace (auto-detected from QuayRegistry CRD)
  --list-orgs          List available organizations and exit
  --execute            Execute the flush (without this, dry-run only)
  --s3-cleanup         Also cleanup orphaned S3/ODF blobs
  -h, --help           Show this help

Auto-detection:
  Kubeconfig   $KUBECONFIG > ~/.kube/config
  Namespace    QuayRegistry CRD > pod labels (quay-component) > pod names
  Database     pod, DB name, and DB user all auto-detected

Examples:
  # Dry-run (uses current $KUBECONFIG)
  export KUBECONFIG=/path/to/kubeconfig
  ./ocp-flush-quay.sh

  # Execute flush on a specific org
  ./ocp-flush-quay.sh --org ocp4 --execute

  # Execute with ODF/S3 cleanup
  ./ocp-flush-quay.sh --org ocp4 --execute --s3-cleanup

  # Override namespace
  ./ocp-flush-quay.sh --org myorg --namespace quay-prod --execute
EOF
}

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

echo "=== Quay $ORG_NAME Organization Flush (OCP) ==="
echo ""

# ── Verify cluster access (oc uses $KUBECONFIG natively) ─────────────
if ! oc cluster-info &>/dev/null; then
    log_error "Cannot connect to cluster. Check \$KUBECONFIG or ~/.kube/config."
    exit 1
fi
log_info "Cluster access: OK"

# ── Auto-detect Quay namespace ───────────────────────────────────────
if [[ -z "$QUAY_NS" ]]; then
    log_info "Auto-detecting Quay namespace..."

    # 1. QuayRegistry CRD (most reliable — works regardless of namespace name)
    QUAY_NS=$(oc get quayregistry -A -o jsonpath='{.items[0].metadata.namespace}' 2>/dev/null || true)

    # 2. Pod labels (quay-component label set by the operator)
    [[ -z "$QUAY_NS" ]] && QUAY_NS=$(oc get pods -A -l quay-component=quay-app \
        -o jsonpath='{.items[0].metadata.namespace}' 2>/dev/null || true)

    # 3. Pod name patterns (covers non-operator deployments)
    [[ -z "$QUAY_NS" ]] && QUAY_NS=$(oc get pods -A 2>/dev/null \
        | grep -iE 'quay.*app|quay.*database' | grep -v clair \
        | awk '{print $1}' | sort | uniq -c | sort -rn | awk '{print $2}' | head -1 || true)

    if [[ -z "$QUAY_NS" ]]; then
        log_error "Could not auto-detect Quay namespace"
        log_info "Set QUAY_NAMESPACE environment variable"
        exit 1
    fi
fi
log_info "Namespace: $QUAY_NS"

# ── Detect PostgreSQL pod ────────────────────────────────────────────
log_info "Detecting PostgreSQL pod..."

# 1. By operator label
PG_POD=$(oc get pods -n "$QUAY_NS" -l quay-component=postgres \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

# 2. By name pattern (covers various naming conventions)
[[ -z "$PG_POD" ]] && PG_POD=$(oc get pods -n "$QUAY_NS" -o name 2>/dev/null \
    | grep -iE 'quay.*database' | grep -v clair | head -1 | sed 's|pod/||')
[[ -z "$PG_POD" ]] && PG_POD=$(oc get pods -n "$QUAY_NS" -o name 2>/dev/null \
    | grep -iE 'database|postgres' | grep -v clair | head -1 | sed 's|pod/||')

if [[ -z "$PG_POD" ]]; then
    log_error "Could not find PostgreSQL pod in namespace $QUAY_NS"
    oc get pods -n "$QUAY_NS"
    exit 1
fi
log_info "PostgreSQL pod: $PG_POD"

# ── Detect database name and user ────────────────────────────────────
# Try the quay-specific user first, fall back to postgres superuser
DB_USER=$(oc exec -n "$QUAY_NS" "$PG_POD" -- psql -U postgres -t \
    -c "SELECT usename FROM pg_user WHERE usename != 'postgres';" 2>/dev/null \
    | grep -i quay | head -1 | tr -d ' \n\r')
[[ -z "$DB_USER" ]] && DB_USER="postgres"

DB_NAME=$(oc exec -n "$QUAY_NS" "$PG_POD" -- psql -U "$DB_USER" -t \
    -c "SELECT datname FROM pg_database WHERE datname != 'postgres' AND datname != 'template0' AND datname != 'template1';" 2>/dev/null \
    | grep -i quay | head -1 | tr -d ' \n\r')
[[ -z "$DB_NAME" ]] && DB_NAME=$(oc exec -n "$QUAY_NS" "$PG_POD" -- psql -U postgres -t \
    -c "SELECT datname FROM pg_database;" 2>/dev/null \
    | grep -i quay | tr -d ' \n\r')
[[ -z "$DB_NAME" ]] && DB_NAME="quay"

log_info "Database: $DB_NAME (user: $DB_USER)"
echo ""

# Helper for psql
run_psql() {
    oc exec -n "$QUAY_NS" "$PG_POD" -- psql -U "$DB_USER" -d "$DB_NAME" "$@"
}

# ── List orgs and exit if requested ───────────────────────────────────
if $LIST_ORGS; then
    log_info "Organizations in $DB_NAME:"
    run_psql -t -c "SELECT username FROM public.user WHERE organization = true ORDER BY username;"
    exit 0
fi

# Get org ID
ORG_ID=$(run_psql -t -c "SELECT id FROM public.user WHERE username = '$ORG_NAME';" 2>/dev/null | tr -d ' \n\r')

if [[ -z "$ORG_ID" ]]; then
    log_error "Organization '$ORG_NAME' not found"
    log_info "Available organizations:"
    run_psql -c "SELECT username FROM public.user WHERE organization = true;"
    exit 1
fi

log_info "Organization: $ORG_NAME (ID: $ORG_ID)"
echo ""

# Get stats
REPO_COUNT=$(run_psql -t -c "SELECT COUNT(*) FROM repository WHERE namespace_user_id = $ORG_ID;" 2>/dev/null | tr -d ' \n\r')
QUOTA_BYTES=$(run_psql -t -c "SELECT COALESCE(size_bytes, 0) FROM quotanamespacesize WHERE namespace_user_id = $ORG_ID;" 2>/dev/null | tr -d ' \n\r')
[[ -z "$QUOTA_BYTES" ]] && QUOTA_BYTES=0
QUOTA_GB=$((QUOTA_BYTES / 1024 / 1024 / 1024))

echo "Repositories: $REPO_COUNT"
echo "Quota: ${QUOTA_GB} GB (${QUOTA_BYTES} bytes)"

if ! $EXECUTE; then
    echo ""
    echo "[DRY-RUN] Use --execute to delete $REPO_COUNT repositories and reset quota"
    $S3_CLEANUP && echo "          --s3-cleanup will auto-detect ODF/S3 config from Quay"
    exit 0
fi

echo ""

# Delete repositories
if [[ "$REPO_COUNT" -gt 0 ]]; then
    echo "=== DELETING REPOSITORIES ==="

    for table in tag manifestchild manifestlabel manifestsecuritystatus \
                 manifestblob manifest uploadedblob quotarepositorysize \
                 repositorysearchscore repositorypermission \
                 repositoryactioncount blobupload accesstoken; do
        echo -n "Deleting from $table... "
        RESULT=$(run_psql -c "DELETE FROM $table WHERE repository_id IN (SELECT id FROM repository WHERE namespace_user_id = $ORG_ID);" 2>/dev/null || true)
        if echo "$RESULT" | grep -q "DELETE"; then
            echo "OK"
        else
            echo "SKIP"
        fi
    done

    echo -n "Deleting repositories... "
    run_psql -c "DELETE FROM repository WHERE namespace_user_id = $ORG_ID;"

    REMAINING=$(run_psql -t -c "SELECT COUNT(*) FROM repository WHERE namespace_user_id = $ORG_ID;" 2>/dev/null | tr -d ' \n\r')
    echo "Remaining: $REMAINING"
else
    echo "No repositories to delete."
fi

# Reset quota
echo ""
echo "=== RESETTING QUOTA ==="
run_psql -c "UPDATE quotanamespacesize SET size_bytes = 0 WHERE namespace_user_id = $ORG_ID;"
NEW_QUOTA=$(run_psql -t -c "SELECT COALESCE(size_bytes, 0) FROM quotanamespacesize WHERE namespace_user_id = $ORG_ID;" 2>/dev/null | tr -d ' \n\r')
echo "New quota: ${NEW_QUOTA} bytes"

# ─── S3 / ODF Cleanup ───────────────────────────────────────────────
if $S3_CLEANUP; then
    echo ""
    echo "=== ODF/S3 STORAGE CLEANUP ==="

    # ── Step 1: Detect storage backend ───────────────────────────────
    #
    # Priority order:
    #   a) ObjectBucketClaim in the Quay namespace (ODF-managed storage)
    #   b) Quay config secret (unmanaged / external S3)

    S3_BUCKET=""
    S3_PATH=""
    S3_ENDPOINT=""

    # --- (a) Try ODF / NooBaa ObjectBucketClaim -----------------------
    log_info "Checking for ObjectBucketClaim (ODF-managed storage)..."
    OBC_NAME=$(oc get obc -n "$QUAY_NS" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

    if [[ -n "$OBC_NAME" ]]; then
        log_info "Found ObjectBucketClaim: $OBC_NAME"

        # Bucket info from the auto-generated ConfigMap (same name as OBC)
        S3_BUCKET=$(oc get cm "$OBC_NAME" -n "$QUAY_NS" \
            -o jsonpath='{.data.BUCKET_NAME}' 2>/dev/null || true)
        BUCKET_HOST=$(oc get cm "$OBC_NAME" -n "$QUAY_NS" \
            -o jsonpath='{.data.BUCKET_HOST}' 2>/dev/null || true)
        BUCKET_PORT=$(oc get cm "$OBC_NAME" -n "$QUAY_NS" \
            -o jsonpath='{.data.BUCKET_PORT}' 2>/dev/null || true)

        # Credentials from the auto-generated Secret (same name as OBC)
        export AWS_ACCESS_KEY_ID=$(oc get secret "$OBC_NAME" -n "$QUAY_NS" \
            -o jsonpath='{.data.AWS_ACCESS_KEY_ID}' 2>/dev/null | base64 -d 2>/dev/null || true)
        export AWS_SECRET_ACCESS_KEY=$(oc get secret "$OBC_NAME" -n "$QUAY_NS" \
            -o jsonpath='{.data.AWS_SECRET_ACCESS_KEY}' 2>/dev/null | base64 -d 2>/dev/null || true)

        # Build endpoint: prefer the external NooBaa S3 route
        S3_ROUTE=$(oc get route s3 -n openshift-storage \
            -o jsonpath='{.spec.host}' 2>/dev/null || true)
        if [[ -n "$S3_ROUTE" ]]; then
            S3_ENDPOINT="https://$S3_ROUTE"
        elif [[ -n "$BUCKET_HOST" ]]; then
            local scheme="https"
            [[ "$BUCKET_PORT" == "80" ]] && scheme="http"
            S3_ENDPOINT="${scheme}://${BUCKET_HOST}"
            [[ -n "$BUCKET_PORT" && "$BUCKET_PORT" != "443" && "$BUCKET_PORT" != "80" ]] && \
                S3_ENDPOINT="${S3_ENDPOINT}:${BUCKET_PORT}"
        fi

        # Storage path from Quay config (default for ODF-managed)
        S3_PATH="datastorage/registry"
    fi

    # --- (b) Fallback: Quay config secret -----------------------------
    if [[ -z "$S3_BUCKET" || -z "$S3_ENDPOINT" ]]; then
        log_info "Extracting S3 config from Quay config secret..."

        # Find the config bundle secret
        local config_secret=""
        # Try from the QuayRegistry CR
        config_secret=$(oc get quayregistry -n "$QUAY_NS" \
            -o jsonpath='{.items[0].spec.configBundleSecret}' 2>/dev/null || true)
        # Fallback: search for it by name pattern
        if [[ -z "$config_secret" ]]; then
            config_secret=$(oc get secrets -n "$QUAY_NS" -o name 2>/dev/null \
                | grep -iE 'config-bundle|quay-config' | head -1 \
                | sed 's|secret/||' || true)
        fi

        if [[ -n "$config_secret" ]]; then
            log_info "Config secret: $config_secret"

            local config_yaml
            config_yaml=$(oc get secret "$config_secret" -n "$QUAY_NS" \
                -o jsonpath='{.data.config\.yaml}' 2>/dev/null \
                | base64 -d 2>/dev/null || true)

            if [[ -n "$config_yaml" ]]; then
                local parsed
                parsed=$(QUAY_CFG="$config_yaml" python3 -c '
import os, re

text = os.environ["QUAY_CFG"]
params = {}
in_storage = False
in_s3 = False
STRIP = chr(34) + chr(39) + " "

for line in text.splitlines():
    s = line.strip()
    if s.startswith("DISTRIBUTED_STORAGE_CONFIG"):
        in_storage = True
        continue
    if in_storage and line and not line[0].isspace() and ":" in s:
        in_storage = False
        in_s3 = False
        continue
    if in_storage and ("S3Storage" in s or "RadosGWStorage" in s):
        in_s3 = True
        continue
    if in_s3:
        m = re.match(
            r"\s+(s3_access_key|s3_secret_key|s3_bucket|"
            r"storage_path|host|hostname|port|is_secure):\s*(.*)", line)
        if m:
            params[m.group(1)] = m.group(2).strip().strip(STRIP)

host = params.get("host", params.get("hostname", ""))
port = params.get("port", "")
secure = params.get("is_secure", "true").lower() in ("true", "yes", "1")
scheme = "https" if secure else "http"
endpoint = scheme + "://" + host
if port and port not in ("443", "80"):
    endpoint += ":" + port

sp = params.get("storage_path", "/datastorage/registry").lstrip("/")

print("_S3_BUCKET=" + params.get("s3_bucket", ""))
print("_S3_PATH=" + sp)
print("_S3_ENDPOINT=" + endpoint)
print("_S3_ACCESS_KEY=" + params.get("s3_access_key", ""))
print("_S3_SECRET_KEY=" + params.get("s3_secret_key", ""))
' 2>/dev/null)

                if [[ -n "$parsed" ]]; then
                    eval "$parsed"
                    S3_BUCKET="${S3_BUCKET:-$_S3_BUCKET}"
                    S3_PATH="${S3_PATH:-$_S3_PATH}"
                    S3_ENDPOINT="${S3_ENDPOINT:-$_S3_ENDPOINT}"
                    export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-$_S3_ACCESS_KEY}"
                    export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-$_S3_SECRET_KEY}"
                fi
            fi
        fi
    fi

    if [[ -z "$S3_BUCKET" || -z "$S3_ENDPOINT" ]]; then
        log_error "Could not detect S3/ODF storage configuration"
        log_error "No ObjectBucketClaim or S3 config found in namespace $QUAY_NS"
        exit 1
    fi

    log_info "S3 Bucket:   $S3_BUCKET"
    log_info "S3 Path:     $S3_PATH"
    log_info "S3 Endpoint: $S3_ENDPOINT"

    # ── Step 2: Find aws CLI ─────────────────────────────────────────
    AWS_CMD=""
    if command -v aws &>/dev/null; then
        AWS_CMD="aws"
        log_info "Using local aws CLI"
    else
        log_warn "aws CLI not found locally"
        log_info "Attempting to run aws from a debug pod on the cluster..."

        # Try to run aws inside an existing pod in openshift-storage
        # (the NooBaa pods have the aws CLI or compatible tooling)
        NOOBAA_POD=$(oc get pods -n openshift-storage -l app=noobaa-endpoint \
            -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

        if [[ -n "$NOOBAA_POD" ]]; then
            log_info "Using NooBaa endpoint pod: $NOOBAA_POD"
            AWS_CMD="oc exec -n openshift-storage $NOOBAA_POD -- aws"
        else
            log_error "aws CLI not available. Install: pip3 install awscli"
            exit 1
        fi
    fi

    # ── Step 3: Identify blobs to keep ───────────────────────────────
    echo ""
    echo "Identifying blobs to keep..."
    run_psql -t -c "SELECT DISTINCT content_checksum FROM imagestorage \
        WHERE id IN (SELECT storage_id FROM manifestblob \
        UNION SELECT storage_id FROM uploadedblob);" 2>/dev/null \
        | tr -d ' ' | grep -v '^$' | sed 's/sha256://' > /tmp/keep_hashes.txt
    KEEP_COUNT=$(wc -l < /tmp/keep_hashes.txt)
    echo "Blobs to keep: $KEEP_COUNT"

    # ── Step 4: List S3 objects ──────────────────────────────────────
    echo "Listing S3 objects (this may take a while)..."
    $AWS_CMD s3 ls "s3://$S3_BUCKET/$S3_PATH/sha256/" --recursive \
        --endpoint-url "$S3_ENDPOINT" --no-verify-ssl 2>/dev/null \
        | awk '{print $4}' > /tmp/all_s3.txt
    S3_COUNT=$(wc -l < /tmp/all_s3.txt)
    echo "Total S3 objects: $S3_COUNT"

    # ── Step 5: Find and delete orphans ──────────────────────────────
    cat /tmp/all_s3.txt | xargs -I{} basename {} > /tmp/s3_hashes.txt
    comm -23 <(sort /tmp/s3_hashes.txt) <(sort /tmp/keep_hashes.txt) \
        > /tmp/delete_hashes.txt
    DELETE_COUNT=$(wc -l < /tmp/delete_hashes.txt)
    echo "Orphaned blobs: $DELETE_COUNT"

    if [[ "$DELETE_COUNT" -eq 0 ]]; then
        echo "No orphaned blobs to delete."
    else
        # Calculate orphan size estimate
        ORPHAN_SIZE=$(grep -f /tmp/delete_hashes.txt /tmp/all_s3.txt \
            | awk '{total += $3} END {printf "%.2f GB", total/1024/1024/1024}' 2>/dev/null || echo "unknown")
        log_info "Estimated orphan size: $ORPHAN_SIZE"

        echo "Building deletion list..."
        > /tmp/delete_paths.txt
        while read hash; do
            grep "/$hash\$" /tmp/all_s3.txt >> /tmp/delete_paths.txt
        done < /tmp/delete_hashes.txt

        echo "Starting S3 deletion in background..."
        nohup bash -c "
            export AWS_ACCESS_KEY_ID=\"$AWS_ACCESS_KEY_ID\"
            export AWS_SECRET_ACCESS_KEY=\"$AWS_SECRET_ACCESS_KEY\"
            DELETED=0
            TOTAL=\$(wc -l < /tmp/delete_paths.txt)
            echo \"Starting deletion of \$TOTAL blobs at \$(date)\" > /tmp/s3_delete.log
            while read path; do
                $AWS_CMD s3 rm \"s3://$S3_BUCKET/\$path\" \
                    --endpoint-url \"$S3_ENDPOINT\" --no-verify-ssl 2>/dev/null
                DELETED=\$((DELETED + 1))
                if [ \$((DELETED % 500)) -eq 0 ]; then
                    echo \"\$(date): Deleted \$DELETED / \$TOTAL\" >> /tmp/s3_delete.log
                fi
            done < /tmp/delete_paths.txt
            $AWS_CMD s3 rm \"s3://$S3_BUCKET/$S3_PATH/uploads/\" --recursive \
                --endpoint-url \"$S3_ENDPOINT\" --no-verify-ssl 2>/dev/null
            echo \"Completed at \$(date): \$DELETED blobs deleted\" >> /tmp/s3_delete.log
        " > /tmp/s3_delete_output.log 2>&1 &

        echo ""
        log_info "S3 deletion running in background"
        log_info "Monitor: tail -f /tmp/s3_delete.log"
    fi
else
    echo ""
    echo "=== DONE ==="
    echo "NOTE: ODF/S3 blobs not cleaned. Use --s3-cleanup to remove orphaned blobs."
    echo "      Do NOT rely on Quay GC alone — it may not reclaim ODF space."
fi
