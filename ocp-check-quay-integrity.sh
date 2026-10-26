#!/bin/bash
# Check Quay blob integrity on OCP-based Quay (operator deployment)
# Usage: ./ocp-check-quay-integrity.sh [options]
#
# Kubeconfig: uses $KUBECONFIG or ~/.kube/config (same as oc)
#
# Requirements: oc, python3
#
# Environment variables:
#   KUBECONFIG      - Path to kubeconfig (standard oc behavior)
#   ORG_NAME        - Organization to check (default: all orgs)
#   QUAY_NAMESPACE  - Quay namespace (auto-detected if not set)
#   BATCH_SIZE      - Blobs per S3 check batch (default: 500)

set -e

# ─── Defaults ────────────────────────────────────────────────────────
ORG_NAME="${ORG_NAME:-}"
QUAY_NS="${QUAY_NAMESPACE:-}"
LIST_ORGS=false
FIX=false
FIX_MISSING=false
AUTHFILE=""
VERBOSE=false
BATCH_SIZE="${BATCH_SIZE:-500}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

# ─── Parse arguments ────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --org)           ORG_NAME="$2"; shift 2 ;;
        --org=*)         ORG_NAME="${1#*=}"; shift ;;
        --namespace)     QUAY_NS="$2"; shift 2 ;;
        --namespace=*)   QUAY_NS="${1#*=}"; shift ;;
        --list-orgs)     LIST_ORGS=true; shift ;;
        --fix)           FIX=true; shift ;;
        --fix-missing)   FIX_MISSING=true; FIX=true; shift ;;
        --authfile)      AUTHFILE="$2"; shift 2 ;;
        --authfile=*)    AUTHFILE="${1#*=}"; shift ;;
        --verbose)       VERBOSE=true; shift ;;
        -h|--help)       usage; exit 0 ;;
        *)               echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

usage() {
    cat << 'EOF'
Usage: ocp-check-quay-integrity.sh [options]

Verify that blobs in an OCP-based Quay actually exist in S3/ODF storage.
Reports (and optionally repairs) missing blobs that cause image pull failures.

Kubeconfig is resolved the standard way: $KUBECONFIG or ~/.kube/config.
Quay namespace, database pod, user, and S3 config are all auto-detected.

Options:
  --org <name>         Check only this organization (default: all orgs)
  --namespace <name>   Quay namespace (auto-detected from QuayRegistry CRD)
  --list-orgs          List available organizations and exit
  --fix                Purge stale DB references for missing blobs so they
                       can be re-pushed or re-mirrored
  --fix-missing        Full repair: flip Mirror→Normal, purge stale refs,
                       re-sync from source, verify, flip back to Mirror.
                       Implies --fix. Pull-secret auto-extracted from cluster.
  --authfile <path>    Auth file for skopeo (auto-extracted if not given)
  --verbose            Show per-blob progress
  -h, --help           Show this help

Environment Variables:
  KUBECONFIG           Kubeconfig path (standard oc behavior)
  ORG_NAME             Same as --org
  QUAY_NAMESPACE       Quay namespace (auto-detected if not set)
  BATCH_SIZE           Blobs per batch (default: 500)

Examples:
  # Scan regional quay (uses current $KUBECONFIG)
  export KUBECONFIG=/path/to/kubeconfig-regional
  ./ocp-check-quay-integrity.sh --org ocp4

  # Full auto-repair
  ./ocp-check-quay-integrity.sh --org ocp4 --fix-missing

  # Just purge stale refs (manual re-mirror after)
  ./ocp-check-quay-integrity.sh --org ocp4 --fix
EOF
}

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()  { echo -e "${CYAN}[STEP]${NC} $1"; }

# ─── Verify cluster access ──────────────────────────────────────────
echo ""
echo "=== Quay Blob Integrity Check (OCP) ==="
echo ""

if ! oc cluster-info &>/dev/null; then
    log_error "Cannot connect to cluster. Check \$KUBECONFIG or ~/.kube/config."
    exit 1
fi
log_info "Cluster access: OK"

# ─── Auto-detect Quay namespace ─────────────────────────────────────
if [[ -z "$QUAY_NS" ]]; then
    log_info "Auto-detecting Quay namespace..."
    QUAY_NS=$(oc get quayregistry -A -o jsonpath='{.items[0].metadata.namespace}' 2>/dev/null || true)
    [[ -z "$QUAY_NS" ]] && QUAY_NS=$(oc get pods -A -l quay-component=quay-app \
        -o jsonpath='{.items[0].metadata.namespace}' 2>/dev/null || true)
    [[ -z "$QUAY_NS" ]] && QUAY_NS=$(oc get pods -A 2>/dev/null \
        | grep -iE 'quay.*app|quay.*database' | grep -v clair \
        | awk '{print $1}' | sort | uniq -c | sort -rn | awk '{print $2}' | head -1 || true)
    if [[ -z "$QUAY_NS" ]]; then
        log_error "Could not auto-detect Quay namespace. Set QUAY_NAMESPACE."
        exit 1
    fi
fi
log_info "Namespace: $QUAY_NS"

# ─── Detect PostgreSQL pod ──────────────────────────────────────────
PG_POD=$(oc get pods -n "$QUAY_NS" -l quay-component=postgres \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
[[ -z "$PG_POD" ]] && PG_POD=$(oc get pods -n "$QUAY_NS" -o name 2>/dev/null \
    | grep -iE 'quay.*database' | grep -v clair | head -1 | sed 's|pod/||')
[[ -z "$PG_POD" ]] && PG_POD=$(oc get pods -n "$QUAY_NS" -o name 2>/dev/null \
    | grep -iE 'database|postgres' | grep -v clair | head -1 | sed 's|pod/||')
if [[ -z "$PG_POD" ]]; then
    log_error "Could not find PostgreSQL pod in namespace $QUAY_NS"
    exit 1
fi
log_info "PostgreSQL pod: $PG_POD"

# ─── Detect Quay app pod ────────────────────────────────────────────
QUAY_POD=$(oc get pods -n "$QUAY_NS" -l quay-component=quay-app \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
[[ -z "$QUAY_POD" ]] && QUAY_POD=$(oc get pods -n "$QUAY_NS" -o name 2>/dev/null \
    | grep -iE 'quay.*app' | head -1 | sed 's|pod/||')
if [[ -z "$QUAY_POD" ]]; then
    log_error "Could not find Quay app pod (needed for S3 access)"
    exit 1
fi
log_info "Quay app pod: $QUAY_POD"

# ─── Detect database name and user ──────────────────────────────────
DB_USER=$(oc exec -n "$QUAY_NS" "$PG_POD" -- psql -U postgres -t \
    -c "SELECT usename FROM pg_user WHERE usename != 'postgres';" 2>/dev/null \
    | grep -i quay | head -1 | tr -d ' \n\r')
[[ -z "$DB_USER" ]] && DB_USER="postgres"

DB_NAME=$(oc exec -n "$QUAY_NS" "$PG_POD" -- psql -U "$DB_USER" -t \
    -c "SELECT datname FROM pg_database WHERE datname NOT IN ('postgres','template0','template1');" 2>/dev/null \
    | grep -i quay | head -1 | tr -d ' \n\r')
[[ -z "$DB_NAME" ]] && DB_NAME=$(oc exec -n "$QUAY_NS" "$PG_POD" -- psql -U postgres -t \
    -c "SELECT datname FROM pg_database;" 2>/dev/null | grep -i quay | tr -d ' \n\r')
[[ -z "$DB_NAME" ]] && DB_NAME="quay"
log_info "Database: $DB_NAME (user: $DB_USER)"

# ─── Helper: run SQL ────────────────────────────────────────────────
run_sql()     { oc exec -n "$QUAY_NS" "$PG_POD" -- psql -U "$DB_USER" -d "$DB_NAME" "$@" 2>/dev/null; }
run_sql_val() { run_sql -t -A -c "$1" | tr -d ' \n\r'; }

# ── List orgs and exit if requested ─────────────────────────────────
if $LIST_ORGS; then
    log_info "Organizations in $DB_NAME:"
    run_sql -t -c "SELECT username FROM public.user WHERE organization = true ORDER BY username;"
    exit 0
fi

# ─── Extract S3 config ──────────────────────────────────────────────
log_step "Extracting S3 storage configuration..."

config_secret=$(oc get secrets -n "$QUAY_NS" -o name 2>/dev/null \
    | grep -iE 'config-secret' | grep -v clair | head -1 | sed 's|secret/||' || true)
[[ -z "$config_secret" ]] && config_secret=$(oc get quayregistry -n "$QUAY_NS" \
    -o jsonpath='{.items[0].spec.configBundleSecret}' 2>/dev/null || true)
[[ -z "$config_secret" ]] && config_secret=$(oc get secrets -n "$QUAY_NS" -o name 2>/dev/null \
    | grep -iE 'config-bundle' | head -1 | sed 's|secret/||' || true)

config_yaml=""
[[ -n "$config_secret" ]] && config_yaml=$(oc get secret "$config_secret" -n "$QUAY_NS" \
    -o jsonpath='{.data.config\.yaml}' 2>/dev/null | base64 -d 2>/dev/null || true)

if [[ -z "$config_yaml" ]]; then
    log_error "Could not read Quay config from secret"
    exit 1
fi

s3_config=$(QUAY_CFG="$config_yaml" python3 -c '
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
        m = re.match(r"\s+(access_key|s3_access_key|secret_key|s3_secret_key|s3_bucket|bucket_name|storage_path|host|hostname|port|is_secure):\s*(.*)", line)
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
    org_id=$(run_sql_val "SELECT id FROM public.user WHERE username = '$ORG_NAME';")
    if [[ -z "$org_id" ]]; then
        log_error "Organization '$ORG_NAME' not found"
        run_sql -c "SELECT username FROM public.user WHERE organization = true;"
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
blob_count=$(run_sql_val "SELECT count(DISTINCT is2.id) FROM imagestorage is2
    $org_filter is2.content_checksum IS NOT NULL;")

echo ""
echo "========================================"
echo "  Blobs to verify: $blob_count"
echo "========================================"
echo ""

[[ "$blob_count" -eq 0 ]] && { log_info "No blobs to check."; exit 0; }

# ─── Dump digests ───────────────────────────────────────────────────
log_step "Extracting blob digests from database..."
digest_file="/tmp/quay_integrity_digests_$$.txt"
run_sql -t -A -c "SELECT DISTINCT is2.content_checksum FROM imagestorage is2
    $org_filter is2.content_checksum IS NOT NULL;" > "$digest_file"
actual_count=$(wc -l < "$digest_file" | tr -d ' ')
log_info "Extracted $actual_count unique digests"

# ─── Generate and upload check script ───────────────────────────────
log_step "Running integrity check inside Quay pod (S3 HEAD per blob)..."
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

oc cp "$check_script" "$QUAY_NS/$QUAY_POD:/tmp/_integrity_check.py"
oc cp "$digest_file"  "$QUAY_NS/$QUAY_POD:/tmp/_integrity_digests.txt"

# ─── Run the check ──────────────────────────────────────────────────
raw_output=$(oc exec -n "$QUAY_NS" "$QUAY_POD" -- bash -c \
    "cat /tmp/_integrity_digests.txt | python3 /tmp/_integrity_check.py" 2>&1)

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
        run_sql -t -A -c "SELECT DISTINCT r.name FROM repository r
            JOIN manifest m ON m.repository_id = r.id
            JOIN manifestblob mb ON mb.manifest_id = m.id
            JOIN imagestorage is2 ON mb.blob_id = is2.id
            WHERE is2.content_checksum = '$digest';" \
            | while read repo; do echo -e "  ${YELLOW}$digest${NC} → $repo"; done
    done
fi

# ─── Fix modes ──────────────────────────────────────────────────────
if $FIX_MISSING && [[ "$total_missing" -gt 0 ]]; then
    echo ""
    echo "========================================"
    echo "  FIX-MISSING: Full Blob Repair"
    echo "========================================"
    echo ""

    # Ensure auth file
    if [[ -z "$AUTHFILE" ]]; then
        AUTHFILE="/tmp/quay_integrity_pullsecret_$$.json"
        log_info "Extracting pull-secret from cluster..."
        oc get secret -n openshift-config pull-secret \
            -o jsonpath='{.data.\.dockerconfigjson}' | base64 -d > "$AUTHFILE" 2>/dev/null
        if [[ ! -s "$AUTHFILE" ]]; then
            log_error "Could not extract pull-secret. Use --authfile."
            exit 1
        fi
    fi

    # Get Quay route
    quay_host=$(oc get route -n "$QUAY_NS" -l quay-component=quay-app \
        -o jsonpath='{.items[0].spec.host}' 2>/dev/null || true)
    [[ -z "$quay_host" ]] && quay_host=$(oc get route -n "$QUAY_NS" \
        -o jsonpath='{.items[0].spec.host}' 2>/dev/null || true)

    log_info "Quay registry: $quay_host"

    # Map missing blobs → repos → tags
    log_step "Mapping missing blobs to repositories and tags..."
    repo_tags_file="/tmp/quay_integrity_repo_tags_$$.txt"
    > "$repo_tags_file"

    for digest in $(echo "$results_json" | python3 -c "
import sys, json
for d in json.load(sys.stdin)['missing_digests']: print(d)
"); do
        run_sql -t -A -c "
            SELECT DISTINCT r.id, u.username || '/' || r.name, t.name
            FROM repository r
            JOIN public.user u ON r.namespace_user_id = u.id
            JOIN manifest m ON m.repository_id = r.id
            JOIN manifestblob mb ON mb.manifest_id = m.id
            JOIN imagestorage is2 ON mb.blob_id = is2.id
            LEFT JOIN tag t ON t.manifest_id = m.id
            WHERE is2.content_checksum = '$digest'
                AND (t.hidden = false OR t.hidden IS NULL)
                AND (t.lifetime_end_ms IS NULL OR t.lifetime_end_ms > (EXTRACT(EPOCH FROM now()) * 1000));
        " | while IFS='|' read -r repo_id repo_full_name tag_name; do
            [[ -n "$repo_id" ]] && echo "$repo_id|$repo_full_name|$tag_name|$digest"
        done >> "$repo_tags_file"
    done

    unique_repos=$(awk -F'|' '{print $1 "|" $2}' "$repo_tags_file" | sort -u)
    repo_count=$(echo "$unique_repos" | grep -c . || true)
    log_info "Affected repositories: $repo_count"

    repos_fixed=0; repos_failed=0; tags_restored=0; tags_failed=0

    while IFS='|' read -r repo_id repo_full_name; do
        [[ -z "$repo_id" ]] && continue
        echo "────────────────────────────────────────"
        log_step "Repo: $repo_full_name (id=$repo_id)"

        mirror_url=$(run_sql_val "SELECT external_reference FROM repomirrorconfig WHERE repository_id = $repo_id;")
        orig_state=$(run_sql_val "SELECT state FROM repository WHERE id = $repo_id;")
        tags=$(grep "^$repo_id|" "$repo_tags_file" | awk -F'|' '{print $3}' | sort -u | grep -v '^$' || true)
        repo_digests=$(grep "^$repo_id|" "$repo_tags_file" | awk -F'|' '{print $4}' | sort -u)

        if [[ -z "$tags" ]]; then
            log_warn "No live tags found — may need manual re-mirror"
            continue
        fi

        log_info "Mirror source: ${mirror_url:-UNKNOWN}"
        log_info "Tags to re-sync: $(echo "$tags" | wc -l | tr -d ' ')"

        if [[ -z "$mirror_url" ]]; then
            log_warn "No mirror source configured — cannot auto-fix"
            repos_failed=$((repos_failed + 1))
            continue
        fi

        # Flip to NORMAL
        log_info "Switching repo to NORMAL..."
        run_sql -c "UPDATE repository SET state = 0 WHERE id = $repo_id;" > /dev/null

        repo_ok=true
        while IFS= read -r tag; do
            [[ -z "$tag" ]] && continue
            echo -n "  Tag $tag: "

            # Purge stale blob refs for this tag
            tag_blob_ids=$(run_sql -t -A -c "
                SELECT DISTINCT is2.id FROM imagestorage is2
                JOIN manifestblob mb ON mb.blob_id = is2.id
                JOIN manifest m ON mb.manifest_id = m.id
                JOIN tag t ON t.manifest_id = m.id
                WHERE t.name = '$tag' AND m.repository_id = $repo_id
                    AND is2.content_checksum IN ($(echo "$repo_digests" | sed "s/^/'/;s/$/'/" | paste -sd,));")

            for blob_id in $tag_blob_ids; do
                run_sql -c "BEGIN;
                    DELETE FROM manifestblob WHERE blob_id = $blob_id;
                    DELETE FROM imagestorageplacement WHERE storage_id = $blob_id;
                    DELETE FROM uploadedblob WHERE blob_id = $blob_id;
                    DELETE FROM imagestoragesignature WHERE storage_id = $blob_id;
                    DELETE FROM imagestorage WHERE id = $blob_id;
                    COMMIT;" > /dev/null 2>&1 || true
            done

            skopeo delete --authfile "$AUTHFILE" "docker://$quay_host/$repo_full_name:$tag" 2>/dev/null || true

            if skopeo copy --authfile "$AUTHFILE" --all \
                "docker://$mirror_url:$tag" "docker://$quay_host/$repo_full_name:$tag" 2>/dev/null; then
                if skopeo inspect --authfile "$AUTHFILE" "docker://$quay_host/$repo_full_name:$tag" > /dev/null 2>&1; then
                    echo -e "${GREEN}OK${NC}"; tags_restored=$((tags_restored + 1))
                else
                    echo -e "${YELLOW}PUSHED but verify failed${NC}"; tags_failed=$((tags_failed + 1)); repo_ok=false
                fi
            else
                echo -e "${RED}FAILED${NC}"; tags_failed=$((tags_failed + 1)); repo_ok=false
            fi
        done <<< "$tags"

        # Flip back
        run_sql -c "UPDATE repository SET state = ${orig_state:-2} WHERE id = $repo_id;" > /dev/null
        $repo_ok && { log_info "✓ repaired"; repos_fixed=$((repos_fixed + 1)); } \
                  || { log_warn "partial repair"; repos_failed=$((repos_failed + 1)); }
        echo ""
    done <<< "$unique_repos"

    echo "========================================"
    echo "  FIX-MISSING RESULTS"
    echo "========================================"
    echo -e "  Repos fixed:   ${GREEN}$repos_fixed${NC}"
    echo -e "  Repos failed:  ${RED}$repos_failed${NC}"
    echo -e "  Tags restored: ${GREEN}$tags_restored${NC}"
    echo -e "  Tags failed:   ${RED}$tags_failed${NC}"
    echo "========================================"
    rm -f "$repo_tags_file"

elif $FIX && [[ "$total_missing" -gt 0 ]]; then
    echo ""
    log_step "Purging stale DB references for missing blobs..."
    fixed=0
    for digest in $(echo "$results_json" | python3 -c "
import sys, json
for d in json.load(sys.stdin)['missing_digests']: print(d)
"); do
        blob_id=$(run_sql_val "SELECT id FROM imagestorage WHERE content_checksum = '$digest';")
        if [[ -n "$blob_id" ]]; then
            run_sql -c "BEGIN;
                DELETE FROM manifestblob WHERE blob_id = $blob_id;
                DELETE FROM imagestorageplacement WHERE storage_id = $blob_id;
                DELETE FROM uploadedblob WHERE blob_id = $blob_id;
                DELETE FROM imagestoragesignature WHERE storage_id = $blob_id;
                DELETE FROM imagestorage WHERE id = $blob_id;
                COMMIT;" && { log_info "Purged: $digest"; fixed=$((fixed + 1)); } \
                           || log_warn "Failed to purge: $digest"
        fi
    done
    echo ""
    log_info "Purged $fixed / $total_missing stale blob references"
    log_info "Use --fix-missing for fully automated repair"

elif [[ "$total_missing" -gt 0 ]]; then
    echo ""
    log_warn "Use --fix to purge stale DB references"
    log_warn "Use --fix-missing for full repair (flip repo, re-sync, verify)"
fi

save_file="/tmp/quay-integrity-$(date +%Y%m%d-%H%M%S).json"
cp "$result_file" "$save_file"
log_info "Full results saved to: $save_file"
rm -f "$digest_file" "$check_script" "$result_file"
