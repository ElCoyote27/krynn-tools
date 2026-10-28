#!/bin/bash
# Flush organization repositories from RHEL-based Quay (container deployment)
# Usage: ./rhel-flush-quay.sh <host> [--org <name>] [--execute] [--s3-cleanup]
#
# Requirements:
#   - SSH access to hostname (passwordless sudo)
#   - python3 on the local machine (for config parsing)

set -e

# ─── Defaults ────────────────────────────────────────────────────────
SSH_HOST=""
ORG_NAME="${ORG_NAME:-ocp4}"
LIST_ORGS=false
EXECUTE=false
S3_CLEANUP=false

# ─── Parse arguments ────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --org)         ORG_NAME="$2"; shift 2 ;;
        --org=*)       ORG_NAME="${1#*=}"; shift ;;
        --list-orgs)   LIST_ORGS=true; shift ;;
        --execute)     EXECUTE=true; shift ;;
        --s3-cleanup)  S3_CLEANUP=true; shift ;;
        -h|--help)     SSH_HOST="__help__"; break ;;
        -*)            echo "Unknown option: $1"; SSH_HOST="__help__"; break ;;
        *)             SSH_HOST="$1"; shift ;;
    esac
done

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

usage() {
    cat << 'EOF'
Usage: rhel-flush-quay.sh <host> [options]

Flush organization repositories from RHEL-based Quay (container deployment).
PostgreSQL container, database, and S3 config are all auto-detected.

Arguments:
  host                 SSH target: alias, hostname, FQDN, or user@host
                       (requires passwordless sudo on the remote)

Options:
  --org <name>         Organization to flush (default: ocp4)
  --list-orgs          List available organizations and exit
  --execute            Execute the flush (without this, dry-run only)
  --s3-cleanup         Also cleanup orphaned S3 blobs
  -h, --help           Show this help

Auto-detection:
  PostgreSQL   container name, DB name, DB user
  S3 config    extracted from Quay container config.yaml

Examples:
  # Dry-run
  ./rhel-flush-quay.sh cquay1

  # Execute flush
  ./rhel-flush-quay.sh cquay1 --org ocp4 --execute

  # Execute with S3 cleanup
  ./rhel-flush-quay.sh cquay1 --org ocp4 --execute --s3-cleanup
EOF
}

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ─── Read Quay config.yaml from the remote host ─────────────────────
read_quay_config() {
    local config_text=""

    # 1. Find the Quay app container (skip postgres / redis / clair)
    #    Handles both Quay 3.15 (quay-rhel8) and 3.16+ (quay-rhel9)
    local quay_ct
    quay_ct=$(ssh "$SSH_HOST" "sudo podman ps --format '{{.Names}}' \
        | grep -iE '^quay$|quay-app|quay-registry' \
        | grep -v postgres | grep -v redis | grep -v clair \
        | head -1" 2>/dev/null || true)
    # Broader fallback: match the quay image itself (rhel8 or rhel9)
    [[ -z "$quay_ct" ]] && quay_ct=$(ssh "$SSH_HOST" \
        "sudo podman ps --format '{{.Names}} {{.Image}}' \
         | grep -iE 'quay/quay-rhel[89]' \
         | awk '{print \$1}' | head -1" 2>/dev/null || true)

    if [[ -n "$quay_ct" ]]; then
        log_info "Quay container: $quay_ct"

        # Try common in-container config paths
        for cpath in /conf/stack/config.yaml \
                     /quay-registry/conf/stack/config.yaml \
                     /config/config.yaml; do
            config_text=$(ssh "$SSH_HOST" \
                "sudo podman exec $quay_ct cat $cpath" 2>/dev/null || true)
            [[ -n "$config_text" ]] && break
        done

        # Try via podman inspect volume mounts
        if [[ -z "$config_text" ]]; then
            local host_path
            host_path=$(ssh "$SSH_HOST" "sudo podman inspect $quay_ct \
                --format '{{range .Mounts}}{{if eq .Destination \"/conf/stack\"}}{{.Source}}{{end}}{{end}}'" \
                2>/dev/null || true)
            if [[ -n "$host_path" ]]; then
                config_text=$(ssh "$SSH_HOST" \
                    "sudo cat ${host_path}/config.yaml" 2>/dev/null || true)
            fi
        fi
    fi

    # 2. Fallback: well-known host paths
    if [[ -z "$config_text" ]]; then
        for hpath in /data/config/config.yaml \
                     /opt/quay/config/config.yaml \
                     /etc/quay/config.yaml; do
            config_text=$(ssh "$SSH_HOST" \
                "sudo cat $hpath" 2>/dev/null || true)
            [[ -n "$config_text" ]] && break
        done
    fi

    echo "$config_text"
}

# ─── Parse S3 settings from Quay config text ────────────────────────
# Outputs shell variable assignments: _S3_BUCKET, _S3_PATH, etc.
parse_s3_config() {
    local config_text="$1"
    # python3 -c with single-quoted script: no single quotes in Python
    # below; chr(39) is used where a literal quote strip is needed.
    QUAY_CFG="$config_text" python3 -c '
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
    if in_storage and ("S3Storage" in s or "RadosGWStorage" in s or "RHOCSStorage" in s):
        in_s3 = True
        continue
    if in_s3:
        m = re.match(
            r"\s+(?:-\s+)?(access_key|s3_access_key|secret_key|s3_secret_key|"
            r"s3_bucket|bucket_name|storage_path|host|hostname|port|is_secure):\s*(.*)", line)
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

print("_S3_BUCKET=" + params.get("s3_bucket", params.get("bucket_name", "")))
print("_S3_PATH=" + sp)
print("_S3_ENDPOINT=" + endpoint)
print("_S3_ACCESS_KEY=" + params.get("access_key", params.get("s3_access_key", "")))
print("_S3_SECRET_KEY=" + params.get("secret_key", params.get("s3_secret_key", "")))
' 2>/dev/null
}

# ─── Resolve S3 config: env-vars win, then auto-detect ──────────────
resolve_s3_config() {
    if [[ -n "$S3_BUCKET" && -n "$S3_ENDPOINT" ]]; then
        log_info "Using S3 config from environment variables"
        return 0
    fi

    log_info "Extracting S3 configuration from Quay on $SSH_HOST..."
    local config_text
    config_text=$(read_quay_config)

    if [[ -z "$config_text" ]]; then
        log_error "Could not read Quay config from $SSH_HOST"
        return 1
    fi

    local parsed
    parsed=$(parse_s3_config "$config_text")

    if [[ -z "$parsed" ]]; then
        log_error "Could not parse S3 settings from Quay config"
        return 1
    fi

    eval "$parsed"

    # Env vars override auto-detected values
    S3_BUCKET="${S3_BUCKET:-$_S3_BUCKET}"
    S3_PATH="${S3_PATH:-$_S3_PATH}"
    S3_ENDPOINT="${S3_ENDPOINT:-$_S3_ENDPOINT}"
    export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-$_S3_ACCESS_KEY}"
    export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-$_S3_SECRET_KEY}"

    if [[ -z "$S3_BUCKET" || -z "$S3_ENDPOINT" ]]; then
        log_error "S3 config incomplete (bucket=$S3_BUCKET endpoint=$S3_ENDPOINT)"
        return 1
    fi

    log_info "S3 Bucket:   $S3_BUCKET"
    log_info "S3 Path:     $S3_PATH"
    log_info "S3 Endpoint: $S3_ENDPOINT"
    return 0
}

# ─── Find aws CLI (local or remote) ─────────────────────────────────
resolve_aws_cmd() {
    if command -v aws &>/dev/null; then
        AWS_CMD="aws"
        log_info "Using local aws CLI"
    elif ssh "$SSH_HOST" "command -v aws" &>/dev/null 2>&1; then
        AWS_CMD="ssh $SSH_HOST aws"
        log_info "Using aws CLI on remote host $SSH_HOST"
    else
        log_error "aws CLI not found locally or on $SSH_HOST"
        log_info "Install: pip3 install awscli  or  dnf install awscli2"
        return 1
    fi
}

# ─────────────────────────────────────────────────────────────────────

[[ -z "$SSH_HOST" || "$SSH_HOST" == "__help__" ]] && { usage; exit 1; }

echo "=== Quay $ORG_NAME Organization Flush (RHEL) ==="
echo ""
log_info "Host: $SSH_HOST"
echo ""

# Detect PostgreSQL container (handles 3.15 rhel8 and 3.16+ rhel9 images)
log_info "Detecting PostgreSQL container..."
PG_CONTAINER=$(ssh "$SSH_HOST" \
    "sudo podman ps --format '{{.Names}}' \
     | grep -iE 'postgresql-quay|postgres.*quay' | head -1" 2>/dev/null || true)
[[ -z "$PG_CONTAINER" ]] && PG_CONTAINER=$(ssh "$SSH_HOST" \
    "sudo podman ps --format '{{.Names}}' \
     | grep -iE 'postgres|pgsql|postgresql' | grep -v clair | head -1" 2>/dev/null || true)
# Broader fallback: match by image name
[[ -z "$PG_CONTAINER" ]] && PG_CONTAINER=$(ssh "$SSH_HOST" \
    "sudo podman ps --format '{{.Names}} {{.Image}}' \
     | grep -iE 'postgresql.*rhel[89]|rhel[89].*postgresql' \
     | grep -v clair | awk '{print \$1}' | head -1" 2>/dev/null || true)

if [[ -z "$PG_CONTAINER" ]]; then
    log_error "Could not find PostgreSQL container on $SSH_HOST"
    log_error "Make sure Quay is running and you have sudo access"
    exit 1
fi
log_info "PostgreSQL container: $PG_CONTAINER"

# Detect database and user
DB_NAME=$(ssh "$SSH_HOST" \
    "sudo podman exec $PG_CONTAINER psql -U postgres -t \
     -c 'SELECT datname FROM pg_database;'" 2>/dev/null \
    | grep -i quay | tr -d ' \n\r')
DB_USER=$(ssh "$SSH_HOST" \
    "sudo podman exec $PG_CONTAINER psql -U postgres -t \
     -c 'SELECT usename FROM pg_user;'" 2>/dev/null \
    | grep -i quay | tr -d ' \n\r')

[[ -z "$DB_NAME" ]] && DB_NAME="quay"
[[ -z "$DB_USER" ]] && DB_USER="quayuser"
log_info "Database: $DB_NAME, User: $DB_USER"

# Build psql prefix for remote execution
PSQL="sudo podman exec $PG_CONTAINER psql -U $DB_USER -d $DB_NAME"

# ── List orgs and exit if requested ───────────────────────────────────
if $LIST_ORGS; then
    log_info "Organizations in $DB_NAME:"
    ssh "$SSH_HOST" \
        "$PSQL -t -c \"SELECT username FROM \\\"user\\\" WHERE organization = true ORDER BY username;\"" \
        2>/dev/null
    exit 0
fi

# Get org ID
ORG_ID=$(ssh "$SSH_HOST" \
    "$PSQL -t -c \"SELECT id FROM \\\"user\\\" WHERE username = '$ORG_NAME';\"" \
    2>/dev/null | tr -d ' \n\r')

if [[ -z "$ORG_ID" ]]; then
    log_error "Organization '$ORG_NAME' not found"
    log_info "Available organizations:"
    ssh "$SSH_HOST" \
        "$PSQL -c \"SELECT username FROM \\\"user\\\" WHERE organization = true;\"" \
        2>/dev/null
    exit 1
fi

log_info "Organization: $ORG_NAME (ID: $ORG_ID)"
echo ""

# Get stats
REPO_COUNT=$(ssh "$SSH_HOST" \
    "$PSQL -t -c 'SELECT COUNT(*) FROM repository WHERE namespace_user_id = $ORG_ID;'" \
    2>/dev/null | tr -d ' \n\r')
QUOTA_BYTES=$(ssh "$SSH_HOST" \
    "$PSQL -t -c 'SELECT COALESCE(size_bytes, 0) FROM quotanamespacesize WHERE namespace_user_id = $ORG_ID;'" \
    2>/dev/null | tr -d ' \n\r')
[[ -z "$QUOTA_BYTES" ]] && QUOTA_BYTES=0
QUOTA_GB=$((QUOTA_BYTES / 1024 / 1024 / 1024))

echo "Repositories: $REPO_COUNT"
echo "Quota: ${QUOTA_GB} GB (${QUOTA_BYTES} bytes)"

if ! $EXECUTE; then
    echo ""
    echo "[DRY-RUN] Use --execute to delete $REPO_COUNT repositories and reset quota"
    $S3_CLEANUP && echo "          --s3-cleanup will auto-detect S3 config from Quay"
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
        RESULT=$(ssh "$SSH_HOST" \
            "$PSQL -c 'DELETE FROM $table WHERE repository_id IN \
             (SELECT id FROM repository WHERE namespace_user_id = $ORG_ID);'" \
            2>/dev/null || true)
        if echo "$RESULT" | grep -q "DELETE"; then
            echo "OK"
        else
            echo "SKIP"
        fi
    done

    echo -n "Deleting repositories... "
    ssh "$SSH_HOST" \
        "$PSQL -c 'DELETE FROM repository WHERE namespace_user_id = $ORG_ID;'" \
        2>/dev/null

    REMAINING=$(ssh "$SSH_HOST" \
        "$PSQL -t -c 'SELECT COUNT(*) FROM repository WHERE namespace_user_id = $ORG_ID;'" \
        2>/dev/null | tr -d ' \n\r')
    echo "Remaining: $REMAINING"
else
    echo "No repositories to delete."
fi

# Reset quota
echo ""
echo "=== RESETTING QUOTA ==="
ssh "$SSH_HOST" \
    "$PSQL -c 'UPDATE quotanamespacesize SET size_bytes = 0 WHERE namespace_user_id = $ORG_ID;'" \
    2>/dev/null
NEW_QUOTA=$(ssh "$SSH_HOST" \
    "$PSQL -t -c 'SELECT COALESCE(size_bytes, 0) FROM quotanamespacesize WHERE namespace_user_id = $ORG_ID;'" \
    2>/dev/null | tr -d ' \n\r')
echo "New quota: ${NEW_QUOTA} bytes"

# ─── S3 Cleanup ─────────────────────────────────────────────────────
if $S3_CLEANUP; then
    echo ""
    echo "=== S3 CLEANUP ==="

    if ! resolve_s3_config; then
        log_error "Cannot proceed with S3 cleanup - config detection failed"
        exit 1
    fi

    if ! resolve_aws_cmd; then
        exit 1
    fi

    # Get blobs still referenced in the database
    echo "Identifying blobs to keep..."
    ssh "$SSH_HOST" \
        "$PSQL -t -c 'SELECT DISTINCT content_checksum FROM imagestorage \
         WHERE id IN (SELECT storage_id FROM manifestblob \
         UNION SELECT storage_id FROM uploadedblob);'" \
        | tr -d ' ' | grep -v '^$' | sed 's/sha256://' > /tmp/keep_hashes.txt
    KEEP_COUNT=$(wc -l < /tmp/keep_hashes.txt)
    echo "Blobs to keep: $KEEP_COUNT"

    # List S3 objects
    echo "Listing S3 objects (this may take a while)..."
    $AWS_CMD s3 ls "s3://$S3_BUCKET/$S3_PATH/sha256/" --recursive \
        --endpoint-url "$S3_ENDPOINT" --no-verify-ssl 2>/dev/null \
        | awk '{print $4}' > /tmp/all_s3.txt
    S3_COUNT=$(wc -l < /tmp/all_s3.txt)
    echo "Total S3 objects: $S3_COUNT"

    # Find orphans
    cat /tmp/all_s3.txt | xargs -I{} basename {} > /tmp/s3_hashes.txt
    comm -23 <(sort /tmp/s3_hashes.txt) <(sort /tmp/keep_hashes.txt) \
        > /tmp/delete_hashes.txt
    DELETE_COUNT=$(wc -l < /tmp/delete_hashes.txt)
    echo "Orphaned blobs: $DELETE_COUNT"

    if [[ "$DELETE_COUNT" -eq 0 ]]; then
        echo "No orphaned blobs to delete."
    else
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
            echo \"Completed at \$(date)\" >> /tmp/s3_delete.log
        " > /tmp/s3_delete_output.log 2>&1 &

        echo ""
        log_info "S3 deletion running in background"
        log_info "Monitor: tail -f /tmp/s3_delete.log"
    fi
else
    echo ""
    echo "=== DONE ==="
    echo "NOTE: S3 blobs not deleted. Use --s3-cleanup to clean orphaned blobs."
    echo "      S3 config will be auto-detected from Quay container config."
fi
