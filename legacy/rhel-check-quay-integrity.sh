#!/bin/bash
# Check Quay blob integrity on RHEL-based Quay (container deployment)
# Usage: ./rhel-check-quay-integrity.sh <host> [options]
#
# Requirements: SSH access with passwordless sudo, python3
#
# Environment variables:
#   ORG_NAME     - Organization to check (default: all orgs)
#   BATCH_SIZE   - Blobs per S3 check batch (default: 500)

set -e

# ─── Defaults ────────────────────────────────────────────────────────
SSH_HOST=""
ORG_NAME="${ORG_NAME:-}"
LIST_ORGS=false
FIX=false
VERBOSE=false
BATCH_SIZE="${BATCH_SIZE:-500}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

# ─── Parse arguments ────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --org)           ORG_NAME="$2"; shift 2 ;;
        --org=*)         ORG_NAME="${1#*=}"; shift ;;
        --list-orgs)     LIST_ORGS=true; shift ;;
        --fix)           FIX=true; shift ;;
        --verbose)       VERBOSE=true; shift ;;
        -h|--help)       SSH_HOST="__help__"; break ;;
        -*)              echo "Unknown option: $1"; SSH_HOST="__help__"; break ;;
        *)               SSH_HOST="$1"; shift ;;
    esac
done

usage() {
    cat << 'EOF'
Usage: rhel-check-quay-integrity.sh <host> [options]

Verify that blobs in a RHEL-based Quay actually exist in S3 storage.
Reports (and optionally repairs) missing blobs that cause image pull failures.

Arguments:
  host                 SSH target: alias, hostname, FQDN, or user@host
                       (requires passwordless sudo on the remote)

Options:
  --org <name>         Check only this organization (default: all orgs)
  --list-orgs          List available organizations and exit
  --fix                Purge stale DB references for missing blobs
                       (prep step before re-running oc-mirror)
  --verbose            Show per-blob progress
  -h, --help           Show this help

Note:
  Central Quay repos are NORMAL, not MIRROR — to restore missing blobs,
  purge stale refs with --fix then re-run oc-mirror from the upstream source.
  For regional (OCP) quays, use ocp-check-quay-integrity.sh --fix-missing.

Examples:
  # Scan central quay
  ./rhel-check-quay-integrity.sh cquay1 --org ocp4

  # Purge stale refs (then re-run oc-mirror)
  ./rhel-check-quay-integrity.sh cquay1 --org ocp4 --fix
EOF
    exit 1
}

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()  { echo -e "${CYAN}[STEP]${NC} $1"; }

[[ -z "$SSH_HOST" || "$SSH_HOST" == "__help__" ]] && usage

echo ""
echo "=== Quay Blob Integrity Check (RHEL) ==="
echo ""

# ─── Verify SSH access ──────────────────────────────────────────────
if ! ssh -o ConnectTimeout=5 "$SSH_HOST" true &>/dev/null; then
    log_error "Cannot SSH to $SSH_HOST"
    exit 1
fi
log_info "SSH access: OK ($SSH_HOST)"

# ─── Detect PostgreSQL container ────────────────────────────────────
log_info "Detecting PostgreSQL container..."
PG_CT=$(ssh "$SSH_HOST" "sudo podman ps --format '{{.Names}}' \
    | grep -iE 'postgresql-quay|postgres.*quay' | head -1" 2>/dev/null || true)
[[ -z "$PG_CT" ]] && PG_CT=$(ssh "$SSH_HOST" \
    "sudo podman ps --format '{{.Names}}' \
     | grep -iE 'postgres|pgsql|postgresql' | grep -v clair | head -1" 2>/dev/null || true)
if [[ -z "$PG_CT" ]]; then
    log_error "Could not find PostgreSQL container on $SSH_HOST"
    exit 1
fi
log_info "PostgreSQL container: $PG_CT"

# ─── Detect database ────────────────────────────────────────────────
DB_NAME=$(ssh "$SSH_HOST" "sudo podman exec $PG_CT psql -U postgres -t \
    -c 'SELECT datname FROM pg_database;'" 2>/dev/null \
    | grep -i quay | tr -d ' \n\r')
DB_USER=$(ssh "$SSH_HOST" "sudo podman exec $PG_CT psql -U postgres -t \
    -c 'SELECT usename FROM pg_user;'" 2>/dev/null \
    | grep -i quay | head -1 | tr -d ' \n\r')
[[ -z "$DB_NAME" ]] && DB_NAME="quay"
[[ -z "$DB_USER" ]] && DB_USER="quayuser"
log_info "Database: $DB_NAME (user: $DB_USER)"

PSQL="sudo podman exec $PG_CT psql -U $DB_USER -d $DB_NAME"

# ─── Helper: run SQL ────────────────────────────────────────────────
run_sql()     { ssh "$SSH_HOST" "$PSQL $*" 2>/dev/null; }
run_sql_val() { ssh "$SSH_HOST" "$PSQL -t -A -c \"$1\"" 2>/dev/null | tr -d ' \n\r'; }

# ── List orgs and exit if requested ─────────────────────────────────
if $LIST_ORGS; then
    log_info "Organizations in $DB_NAME:"
    ssh "$SSH_HOST" "$PSQL -t -c \"SELECT username FROM public.\\\"user\\\" WHERE organization = true ORDER BY username;\"" 2>/dev/null
    exit 0
fi

# ─── Detect Quay container ──────────────────────────────────────────
QUAY_CT=$(ssh "$SSH_HOST" "sudo podman ps --format '{{.Names}}' \
    | grep -iE '^quay$|quay-app|quay-registry' \
    | grep -v postgres | grep -v redis | grep -v clair | head -1" 2>/dev/null || true)
[[ -z "$QUAY_CT" ]] && QUAY_CT=$(ssh "$SSH_HOST" \
    "sudo podman ps --format '{{.Names}} {{.Image}}' \
     | grep -iE 'quay/quay-rhel[89]' | awk '{print \$1}' | head -1" 2>/dev/null || true)

# ─── Extract S3 config ──────────────────────────────────────────────
log_step "Extracting S3 configuration from Quay..."
config_text=""
if [[ -n "$QUAY_CT" ]]; then
    for cpath in /conf/stack/config.yaml /quay-registry/conf/stack/config.yaml /config/config.yaml; do
        config_text=$(ssh "$SSH_HOST" "sudo podman exec $QUAY_CT cat $cpath" 2>/dev/null || true)
        [[ -n "$config_text" ]] && break
    done
fi
if [[ -z "$config_text" ]]; then
    for hpath in /data/config/config.yaml /opt/quay/config/config.yaml /etc/quay/config.yaml; do
        config_text=$(ssh "$SSH_HOST" "sudo cat $hpath" 2>/dev/null || true)
        [[ -n "$config_text" ]] && break
    done
fi
if [[ -z "$config_text" ]]; then
    log_error "Could not read Quay config from $SSH_HOST"
    exit 1
fi

s3_config=$(QUAY_CFG="$config_text" python3 -c '
import os, re
text = os.environ["QUAY_CFG"]
params = {}
in_storage = in_s3 = False
STRIP = chr(34) + chr(39) + " "
for line in text.splitlines():
    s = line.strip()
    if s.startswith("DISTRIBUTED_STORAGE_CONFIG"):
        in_storage = True; continue
    if in_storage and line and not line[0].isspace() and ":" in s:
        in_storage = in_s3 = False; continue
    if in_storage and ("S3Storage" in s or "RadosGWStorage" in s or "RHOCSStorage" in s):
        in_s3 = True; continue
    if in_s3:
        m = re.match(r"\s+(?:-\s+)?(access_key|s3_access_key|secret_key|s3_secret_key|s3_bucket|bucket_name|storage_path|host|hostname|port|is_secure):\s*(.*)", line)
        if m: params[m.group(1)] = m.group(2).strip().strip(STRIP)
host = params.get("host", params.get("hostname", ""))
port = params.get("port", "")
secure = params.get("is_secure", "true").lower() in ("true", "yes", "1")
endpoint = ("https" if secure else "http") + "://" + host
if port and port not in ("443", "80"): endpoint += ":" + port
sp = params.get("storage_path", "/datastorage/registry").lstrip("/")
ak = params.get("access_key", params.get("s3_access_key", ""))
sk = params.get("secret_key", params.get("s3_secret_key", ""))
bk = params.get("s3_bucket", params.get("bucket_name", ""))
print(f"{ak}\n{sk}\n{bk}\n{sp}\n{endpoint}")
' 2>/dev/null)

if [[ -z "$s3_config" ]]; then
    log_error "Could not parse S3 config from Quay configuration"
    exit 1
fi

S3_ACCESS_KEY=$(echo "$s3_config" | sed -n '1p')
S3_SECRET_KEY=$(echo "$s3_config" | sed -n '2p')
S3_BUCKET=$(echo "$s3_config" | sed -n '3p')
S3_PATH=$(echo "$s3_config" | sed -n '4p')
S3_ENDPOINT=$(echo "$s3_config" | sed -n '5p')

log_info "S3 Bucket:   $S3_BUCKET"
log_info "S3 Path:     $S3_PATH"
log_info "S3 Endpoint: $S3_ENDPOINT"

# ─── Build org filter ───────────────────────────────────────────────
org_filter="WHERE"
if [[ -n "$ORG_NAME" ]]; then
    org_id=$(run_sql_val "SELECT id FROM public.\\\"user\\\" WHERE username = '$ORG_NAME';")
    if [[ -z "$org_id" ]]; then
        log_error "Organization '$ORG_NAME' not found"
        exit 1
    fi
    log_info "Organization: $ORG_NAME (ID: $org_id)"
    org_filter="JOIN manifestblob mb ON mb.blob_id = is2.id
                JOIN manifest m ON mb.manifest_id = m.id
                JOIN repository r ON m.repository_id = r.id
                WHERE r.namespace_user_id = $org_id AND"
else
    log_info "Organization: ALL"
fi

# ─── Count blobs ────────────────────────────────────────────────────
log_step "Counting blobs to verify..."
blob_count=$(ssh "$SSH_HOST" "$PSQL -t -A \
    -c \"SELECT count(DISTINCT is2.id) FROM imagestorage is2
        $org_filter is2.content_checksum IS NOT NULL;\"" 2>/dev/null | tr -d ' \n\r')

echo ""
echo "========================================"
echo "  Blobs to verify: $blob_count"
echo "========================================"
echo ""

[[ "$blob_count" -eq 0 ]] && { log_info "No blobs to check."; exit 0; }

# ─── Dump digests ───────────────────────────────────────────────────
log_step "Extracting blob digests from database..."
digest_file="/tmp/quay_integrity_digests_$$.txt"
ssh "$SSH_HOST" "$PSQL -t -A \
    -c \"SELECT DISTINCT is2.content_checksum FROM imagestorage is2
        $org_filter is2.content_checksum IS NOT NULL;\"" \
    > "$digest_file" 2>/dev/null
actual_count=$(wc -l < "$digest_file" | tr -d ' ')
log_info "Extracted $actual_count unique digests"

# ─── Generate check script ──────────────────────────────────────────
log_step "Running integrity check..."
verbose_py="False"; $VERBOSE && verbose_py="True"
check_script="/tmp/quay_integrity_check_$$.py"

cat << PYEOF > "$check_script"
import boto3, botocore, sys, json, urllib3
urllib3.disable_warnings()
s3 = boto3.client('s3', endpoint_url="${S3_ENDPOINT}",
    aws_access_key_id="${S3_ACCESS_KEY}", aws_secret_access_key="${S3_SECRET_KEY}", verify=False)
BUCKET, PREFIX, BATCH = "${S3_BUCKET}", "${S3_PATH}", ${BATCH_SIZE}
VERBOSE = ${verbose_py}
missing, checked, ok_count, errors = [], 0, 0, 0
for line in sys.stdin:
    cs = line.strip()
    if not cs or not cs.startswith('sha256:'): continue
    digest = cs[7:]
    key = f"{PREFIX}/sha256/{digest[:2]}/{digest}"
    try:
        s3.head_object(Bucket=BUCKET, Key=key); ok_count += 1
    except botocore.exceptions.ClientError as e:
        code = e.response['Error']['Code']
        if code in ('404', 'NoSuchKey'): missing.append(cs)
        else:
            errors += 1
            if VERBOSE: print(f"S3_ERROR {cs} {code}", file=sys.stderr, flush=True)
    except Exception as e:
        errors += 1
        if VERBOSE: print(f"S3_ERROR {cs} {e}", file=sys.stderr, flush=True)
    checked += 1
    if checked % BATCH == 0: print(f"PROGRESS {checked} {len(missing)} {errors}", flush=True)
print(f"PROGRESS {checked} {len(missing)} {errors}", flush=True)
print("RESULTS_START", flush=True)
print(json.dumps({"checked": checked, "ok": ok_count, "missing": len(missing),
    "errors": errors, "missing_digests": missing}), flush=True)
print("RESULTS_END", flush=True)
PYEOF

# ─── Run: prefer remote, fallback to container, then local ──────────
run_remote=false
if ssh "$SSH_HOST" "python3 -c 'import boto3'" &>/dev/null 2>&1; then
    run_remote=true
elif [[ -n "$QUAY_CT" ]] && ssh "$SSH_HOST" "sudo podman exec $QUAY_CT python3 -c 'import boto3'" &>/dev/null 2>&1; then
    run_remote="container"
fi

if [[ "$run_remote" == "true" ]]; then
    log_info "Running check on remote host"
    scp -q "$check_script" "$SSH_HOST:/tmp/_integrity_check.py"
    scp -q "$digest_file"  "$SSH_HOST:/tmp/_integrity_digests.txt"
    raw_output=$(ssh "$SSH_HOST" \
        "cat /tmp/_integrity_digests.txt | python3 /tmp/_integrity_check.py" 2>&1)
elif [[ "$run_remote" == "container" ]]; then
    log_info "Running check inside Quay container"
    scp -q "$check_script" "$SSH_HOST:/tmp/_integrity_check.py"
    scp -q "$digest_file"  "$SSH_HOST:/tmp/_integrity_digests.txt"
    ssh "$SSH_HOST" "sudo podman cp /tmp/_integrity_check.py $QUAY_CT:/tmp/"
    ssh "$SSH_HOST" "sudo podman cp /tmp/_integrity_digests.txt $QUAY_CT:/tmp/"
    raw_output=$(ssh "$SSH_HOST" \
        "sudo podman exec $QUAY_CT bash -c 'cat /tmp/_integrity_digests.txt | python3 /tmp/_integrity_check.py'" 2>&1)
elif python3 -c "import boto3" &>/dev/null 2>&1; then
    log_info "Running check locally"
    raw_output=$(cat "$digest_file" | python3 "$check_script" 2>&1)
else
    log_error "python3+boto3 not available locally, on remote host, or in Quay container"
    log_info "Install: pip3 install boto3"
    exit 1
fi

# ─── Parse progress ─────────────────────────────────────────────────
while IFS= read -r line; do
    if [[ "$line" == PROGRESS* ]]; then
        read -r _ p_checked p_missing p_errors <<< "$line"
        printf "\r  Checked: %s / %s | Missing: %s | Errors: %s" \
            "$p_checked" "$actual_count" "$p_missing" "$p_errors"
    fi
done <<< "$raw_output"
echo ""

results_json=$(echo "$raw_output" | sed -n '/RESULTS_START/,/RESULTS_END/p' | grep -v RESULTS_)
if [[ -z "$results_json" ]]; then
    log_error "Failed to get results from integrity check"
    echo "$raw_output"
    exit 1
fi

result_file="/tmp/quay_integrity_results_$$.json"
echo "$results_json" > "$result_file"

# ─── Display results ────────────────────────────────────────────────
total_checked=$(echo "$results_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['checked'])")
total_ok=$(echo "$results_json"      | python3 -c "import sys,json; print(json.load(sys.stdin)['ok'])")
total_missing=$(echo "$results_json"  | python3 -c "import sys,json; print(json.load(sys.stdin)['missing'])")
total_errors=$(echo "$results_json"   | python3 -c "import sys,json; print(json.load(sys.stdin)['errors'])")

echo ""
echo "========================================"
echo "  INTEGRITY CHECK RESULTS"
echo "========================================"
echo -e "  Checked:  ${CYAN}$total_checked${NC}"
echo -e "  OK:       ${GREEN}$total_ok${NC}"
echo -e "  Missing:  ${RED}$total_missing${NC}"
echo -e "  Errors:   ${YELLOW}$total_errors${NC}"
echo "========================================"

if [[ "$total_missing" -eq 0 && "$total_errors" -eq 0 ]]; then
    echo ""
    log_info "All blobs verified successfully ✓"
    rm -f "$digest_file" "$check_script" "$result_file"
    exit 0
fi

# ─── List missing blobs ─────────────────────────────────────────────
if [[ "$total_missing" -gt 0 ]]; then
    echo ""
    log_warn "Missing blobs ($total_missing):"
    echo "$results_json" | python3 -c "
import sys, json
for d in json.load(sys.stdin)['missing_digests']: print(f'  {d}')
"
    echo ""
    log_info "Affected images:"
    for digest in $(echo "$results_json" | python3 -c "
import sys, json
for d in json.load(sys.stdin)['missing_digests']: print(d)
"); do
        ssh "$SSH_HOST" "$PSQL -t -A \
            -c \"SELECT DISTINCT r.name FROM repository r
                JOIN manifest m ON m.repository_id = r.id
                JOIN manifestblob mb ON mb.manifest_id = m.id
                JOIN imagestorage is2 ON mb.blob_id = is2.id
                WHERE is2.content_checksum = '$digest';\"" 2>/dev/null \
            | while read repo; do echo -e "  ${YELLOW}$digest${NC} → $repo"; done
    done
fi

# ─── Fix mode ───────────────────────────────────────────────────────
if $FIX && [[ "$total_missing" -gt 0 ]]; then
    echo ""
    log_step "Purging stale DB references for missing blobs..."
    fixed=0
    for digest in $(echo "$results_json" | python3 -c "
import sys, json
for d in json.load(sys.stdin)['missing_digests']: print(d)
"); do
        blob_id=$(run_sql_val "SELECT id FROM imagestorage WHERE content_checksum = '$digest';")
        if [[ -n "$blob_id" ]]; then
            ssh "$SSH_HOST" "$PSQL -c \"
                BEGIN;
                DELETE FROM manifestblob WHERE blob_id = $blob_id;
                DELETE FROM imagestorageplacement WHERE storage_id = $blob_id;
                DELETE FROM uploadedblob WHERE blob_id = $blob_id;
                DELETE FROM imagestoragesignature WHERE storage_id = $blob_id;
                DELETE FROM imagestorage WHERE id = $blob_id;
                COMMIT;\"" 2>/dev/null \
                && { log_info "Purged: $digest"; fixed=$((fixed + 1)); } \
                || log_warn "Failed to purge: $digest"
        fi
    done
    echo ""
    log_info "Purged $fixed / $total_missing stale blob references"
    log_info "Re-run oc-mirror to restore the actual blob data"

elif [[ "$total_missing" -gt 0 ]]; then
    echo ""
    log_warn "Use --fix to purge stale DB references, then re-run oc-mirror"
fi

save_file="/tmp/quay-integrity-$(date +%Y%m%d-%H%M%S).json"
cp "$result_file" "$save_file"
log_info "Full results saved to: $save_file"
rm -f "$digest_file" "$check_script" "$result_file"
