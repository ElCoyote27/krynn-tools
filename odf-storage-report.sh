#!/bin/bash
# odf-storage-report.sh v1.10
#
# ODF storage consumption breakdown for OpenShift clusters.
#
# Generates a read-only report covering:
#   - Ceph cluster health, pool usage, OSD distribution
#   - StorageCluster resource configuration
#   - ODF node CPU allocation
#   - PVC inventory across storage-related namespaces
#   - Per-org Quay registry breakdown (if Quay is deployed)
#   - ACM Observability spoke metrics storage (if ACM is deployed)
#
# Supports:
#   - Clusters with ODF only (no Quay, no ACM)
#   - Clusters without ODF (LVMS, SNO) — gracefully skips ODF sections
#   - Ceph tools pod optional — reports what it can without it
#   - RGW bucket-level breakdown via radosgw-admin (if ceph tools enabled)
#   - Quay 3.15 and 3.16 (auto-detected)
#   - ACM with MultiClusterObservability (auto-detected)
#
# Read-only: does NOT modify anything on the cluster.
#
# Usage:
#   export KUBECONFIG=/path/to/your-cluster-kubeconfig
#   ./odf-storage-report.sh
#
# Requirements:
#   - oc (OpenShift CLI) in PATH
#   - python3 in PATH
#   - Ceph tools pod recommended (enableCephTools: true) but not required
#
# License: Apache-2.0
#
# Maintainers & Contributors:
#   Vincent Cojot & Johann Peyrard
#

[ "$BASH" ] && function whence { type -p "$@"; }
PATH_SCRIPT="$(cd $(/usr/bin/dirname $(whence -- $0 || echo $0));pwd)"

# --- Colours (if terminal) ---
if [ -t 1 ]; then
	C_HDR="\e[1;36m"   # cyan bold
	C_OK="\e[1;32m"    # green
	C_WRN="\e[1;33m"   # yellow
	C_ERR="\e[1;31m"   # red
	C_RST="\e[0m"
else
	C_HDR="" C_OK="" C_WRN="" C_ERR="" C_RST=""
fi
hdr()  { echo -e "\n${C_HDR}=== $* ===${C_RST}"; }
info() { echo -e "(II) $*"; }
warn() { echo -e "${C_WRN}(WW) $*${C_RST}"; }
err()  { echo -e "${C_ERR}(EE) $*${C_RST}"; }

# --- Pre-flight: KUBECONFIG ---
if [[ -z "${KUBECONFIG}" ]]; then
	err "KUBECONFIG is not set."
	echo "    Export the hub cluster kubeconfig before running this script, e.g.:"
	echo "      export KUBECONFIG=/path/to/kubeconfig"
	exit 1
fi
if [[ ! -f "${KUBECONFIG}" ]]; then
	err "KUBECONFIG=${KUBECONFIG} does not exist."
	exit 1
fi

# --- Pre-flight: cluster reachability ---
HUB_API=$(oc whoami --show-server 2>/dev/null || true)
if [[ -z "${HUB_API}" ]]; then
	err "Cannot reach cluster via KUBECONFIG=${KUBECONFIG}"
	err "Check your VPN, proxy, and certificate settings."
	exit 1
fi
HUB_FQDN=$(echo "${HUB_API}" | sed -E 's|https?://api\.||; s|:[0-9]+/?$||')
HUB_SHORT=$(echo "${HUB_FQDN}" | cut -d. -f1)
info "Connected to: ${HUB_SHORT} (${HUB_API})"

# --- Check for ODF (openshift-storage namespace + StorageCluster) ---
HAS_ODF=false
HAS_STORAGECLUSTER=false
SC_NAME=""
if oc get namespace openshift-storage &>/dev/null; then
	HAS_ODF=true
	SC_NAME=$(oc get storagecluster -n openshift-storage \
		-o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
	if [[ -n "${SC_NAME}" ]]; then
		HAS_STORAGECLUSTER=true
	fi
fi

if ! ${HAS_ODF}; then
	info "ODF not deployed (openshift-storage namespace not found)."
	info "This cluster may use LVMS or another storage backend."
elif ! ${HAS_STORAGECLUSTER}; then
	info "No ODF StorageCluster found. The openshift-storage namespace"
	info "exists but may be used by other components (e.g. Observability)."
fi

# --- Discover ceph tools pod ---
CEPH_TOOLS=false
TOOLS_POD=""
if ${HAS_STORAGECLUSTER}; then
	TOOLS_POD=$(oc get pods -n openshift-storage -l app=rook-ceph-tools \
		-o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
	if [[ -n "${TOOLS_POD}" ]]; then
		CEPH_TOOLS=true
	else
		warn "No rook-ceph-tools pod found."
		warn "Enable it for deeper Ceph and RGW bucket insights:"
		warn "  oc patch storagecluster ${SC_NAME} -n openshift-storage"
		warn "    --type merge -p '{\"spec\":{\"enableCephTools\":true}}'"
		warn "For more information:"
		warn "  oc explain storageclusters.ocs.openshift.io.spec.enableCephTools"
	fi
fi

ceph_cmd() {
	if ! ${CEPH_TOOLS}; then return 1; fi
	oc exec -n openshift-storage "${TOOLS_POD}" -- "$@" 2>/dev/null
}

if ${CEPH_TOOLS}; then
	# ==================================================================
	hdr "CEPH CLUSTER HEALTH — ${HUB_SHORT}"
	# ==================================================================
	ceph_cmd ceph status

	# ==================================================================
	hdr "CEPH POOL USAGE"
	# ==================================================================
	ceph_cmd ceph df

	# ==================================================================
	hdr "OSD DISTRIBUTION & UTILISATION"
	# ==================================================================
	ceph_cmd ceph osd df tree

	# ==================================================================
	hdr "POOL PG DISTRIBUTION"
	# ==================================================================
	ceph_cmd ceph osd pool ls detail -f json 2>/dev/null | python3 -c "
import json, sys
try:
    pools = json.load(sys.stdin)
except Exception:
    pools = []
if not pools:
    print('  (no pool data available)')
    sys.exit(0)
fmt = '  {:<60s}  {:>6}  {:>6}  {:>10}  {:>10}'
print(fmt.format('POOL', 'PG_NUM', 'PGP', 'AUTOSCALE', 'CRUSH RULE'))
print('  ' + '-' * 98)
for p in pools:
    print(fmt.format(
        p['pool_name'], p['pg_num'], p.get('pg_placement_num', p['pg_num']),
        p.get('pg_autoscale_mode', '?'),
        str(p.get('crush_rule', '?'))))
" 2>/dev/null
fi

if ${HAS_STORAGECLUSTER}; then

# ======================================================================
hdr "STORAGECLUSTER RESOURCES"
# ======================================================================
SC_JSON=$(oc get storagecluster "${SC_NAME}" -n openshift-storage -o json 2>/dev/null)
if [[ -n "${SC_JSON}" ]]; then
	echo "${SC_JSON}" | python3 -c "
import json, sys
sc = json.load(sys.stdin)
spec = sc.get('spec', {})
phase = sc.get('status', {}).get('phase', 'unknown')
res = spec.get('resources', {})
ds = spec.get('storageDeviceSets', [{}])[0]
mcg = spec.get('multiCloudGateway', {}).get('endpoints', {})

print(f'  Phase:          {phase}')
print(f'  resourceProfile: {spec.get(\"resourceProfile\", \"(default)\")}')
print(f'  deviceSet count: {ds.get(\"count\", \"?\")}  replica: {ds.get(\"replica\", \"?\")}')
print()

fmt = '  {:<20s} {:>8s} / {:<8s}  {:>8s} / {:<8s}'
print(fmt.format('COMPONENT', 'CPU req', 'limit', 'MEM req', 'limit'))
print('  ' + '-' * 68)
for name in ['mds','mgr','mon','rgw','noobaa-core','noobaa-db','noobaa-endpoint']:
    r = res.get(name, {})
    req = r.get('requests', {})
    lim = r.get('limits', {})
    print(fmt.format(name,
        str(req.get('cpu','-')), str(lim.get('cpu','-')),
        str(req.get('memory','-')), str(lim.get('memory','-'))))

osd_r = ds.get('resources', {})
osd_req = osd_r.get('requests', {})
osd_lim = osd_r.get('limits', {})
print(fmt.format('osd (deviceSet)',
    str(osd_req.get('cpu','-')), str(osd_lim.get('cpu','-')),
    str(osd_req.get('memory','-')), str(osd_lim.get('memory','-'))))

if mcg:
    print(f\"\"\"
  MCG endpoints:    min={mcg.get('minCount','?')} max={mcg.get('maxCount','?')}\"\"\")
" 2>/dev/null
else
	info "No StorageCluster resource found."
fi

# ======================================================================
hdr "ODF NODE CPU ALLOCATION"
# ======================================================================
ODF_NODES=$(oc get nodes -l cluster.ocs.openshift.io/openshift-storage -o name 2>/dev/null)
if [[ -n "${ODF_NODES}" ]]; then
	printf "  %-45s  %16s  %16s\n" "NODE" "CPU REQUESTS" "CPU LIMITS"
	echo "  $(printf '%0.s-' {1..80})"
	for node in ${ODF_NODES}; do
		nname=$(echo "$node" | sed 's|node/||')
		alloc=$(oc describe "$node" 2>/dev/null | grep -A5 'Allocated resources' | grep cpu | head -1 | awk '{print $2, $3, $4, $5}')
		req=$(echo "$alloc" | awk '{print $1, $2}')
		lim=$(echo "$alloc" | awk '{print $3, $4}')
		printf "  %-45s  %16s  %16s\n" "$nname" "$req" "$lim"
	done
else
	info "No ODF-labelled nodes found (cluster.ocs.openshift.io/openshift-storage)."
fi

# ======================================================================
hdr "PVC INVENTORY (openshift-storage)"
# ======================================================================
oc get pvc -n openshift-storage --no-headers -o custom-columns=\
'NAME:.metadata.name,CAPACITY:.status.capacity.storage,SC:.spec.storageClassName,STATUS:.status.phase' \
	2>/dev/null | sort -k2 -h

# ======================================================================
hdr "RADOSGW BUCKET USAGE"
# ======================================================================
RGW_ZONE=""
if ${CEPH_TOOLS}; then
	RGW_ZONE=$(ceph_cmd radosgw-admin zone list 2>/dev/null | \
		python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    zones = [z for z in d.get('zones', []) if z != 'default']
    if zones:
        print(zones[0])
except Exception:
    pass
" 2>/dev/null)
fi

if ! ${CEPH_TOOLS}; then
	info "Ceph tools pod not available. Enable it for bucket breakdown."
elif [[ -z "${RGW_ZONE}" ]]; then
	info "No RGW zone detected. Skipping bucket breakdown."
else
	BUCKET_JSON=$(ceph_cmd radosgw-admin bucket stats \
		--rgw-zone="${RGW_ZONE}" 2>/dev/null)

	# Collect NooBaa backing-store target buckets (CRD may not exist)
	NOOBAA_BS_JSON=$(oc get backingstore -n openshift-storage \
		-o json 2>/dev/null || echo '{"items":[]}')
	# Collect OBCs cluster-wide for consumer mapping (CRD may not exist)
	OBC_JSON=$(oc get obc -A -o json 2>/dev/null || echo '{"items":[]}')
	# Collect OBC ConfigMaps for bucket-name resolution
	OBC_BUCKETS=""
	for obc_line in $(echo "${OBC_JSON}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for i in data.get('items', []):
    ns = i['metadata']['namespace']
    nm = i['metadata']['name']
    sc = i['spec'].get('storageClassName','')
    print(f'{ns}|{nm}|{sc}')
" 2>/dev/null); do
		obc_ns=$(echo "${obc_line}" | cut -d'|' -f1)
		obc_nm=$(echo "${obc_line}" | cut -d'|' -f2)
		obc_sc=$(echo "${obc_line}" | cut -d'|' -f3)
		obc_bkt=$(oc get configmap "${obc_nm}" -n "${obc_ns}" \
			-o jsonpath='{.data.BUCKET_NAME}' 2>/dev/null || true)
		OBC_BUCKETS="${OBC_BUCKETS}${obc_ns}|${obc_nm}|${obc_sc}|${obc_bkt}"$'\n'
	done

	echo "${BUCKET_JSON}" | python3 -c "
import json, sys, os

def pretty(b):
    if b >= 1024**4: return f'{b/1024**4:.2f} TiB'
    if b >= 1024**3: return f'{b/1024**3:.1f} GiB'
    if b >= 1024**2: return f'{b/1024**2:.0f} MiB'
    if b >= 1024:    return f'{b/1024:.0f} KiB'
    return f'{b} B'

try:
    data = json.load(sys.stdin)
except Exception:
    data = []

if not data:
    print('  (no buckets found in this zone)')
    sys.exit(0)

# NooBaa backing-store target buckets
noobaa_targets = set()
try:
    bs = json.loads('''${NOOBAA_BS_JSON}''')
    for item in bs.get('items', []):
        tb = item.get('spec',{}).get('s3Compatible',{}).get('targetBucket','')
        if tb:
            noobaa_targets.add(tb)
except Exception:
    pass

# OBC consumer mapping: bucket_name -> [ns/obc-name, ...]
obc_map = {}       # direct RGW bucket -> consumer label
noobaa_obcs = []   # OBCs going through NooBaa
obc_lines = '''${OBC_BUCKETS}'''.strip().splitlines()
for line in obc_lines:
    parts = line.split('|', 3)
    if len(parts) < 4:
        continue
    ns, nm, sc, bkt = parts
    label = f'{ns}/{nm}'
    if 'noobaa' in sc:
        noobaa_obcs.append(label)
    elif bkt:
        obc_map[bkt] = label

def identify(bucket_name, owner):
    # Direct RGW OBC match
    if bucket_name in obc_map:
        return obc_map[bucket_name]
    # NooBaa backing-store bucket
    if bucket_name in noobaa_targets:
        consumers = ', '.join(noobaa_obcs) if noobaa_obcs else ''
        tag = 'NooBaa backing store'
        if consumers:
            tag += f' ({consumers})'
        return tag
    # Heuristic fallback from owner/name
    if 'prometheus' in owner or bucket_name.startswith('grafana-'):
        return 'Grafana/Prometheus'
    return owner

buckets = []
for b in data:
    name   = b.get('bucket', '?')
    owner  = b.get('owner', '?')
    usage  = b.get('usage', {}).get('rgw.main', {})
    size   = usage.get('size', 0)
    objs   = usage.get('num_objects', 0)
    consumer = identify(name, owner)
    buckets.append((size, objs, consumer, name))

buckets.sort(key=lambda x: x[0], reverse=True)

w_name = max(max(len(t[3]) for t in buckets), 6)
w_cons = max(max(len(t[2]) for t in buckets), 8)

hdr = f'  {\"BUCKET\":<{w_name}}  {\"SIZE\":>10}  ' \
      f'{\"OBJECTS\":>9}  {\"CONSUMER\":<{w_cons}}'
print(hdr)
print('  ' + '-' * (len(hdr) - 2))

total_size = 0
total_objs = 0
for size, objs, consumer, name in buckets:
    total_size += size
    total_objs += objs
    print(f'  {name:<{w_name}}  {pretty(size):>10}  '
          f'{objs:>9}  {consumer:<{w_cons}}')

print('  ' + '-' * (len(hdr) - 2))
print(f'  {\"TOTAL\":<{w_name}}  {pretty(total_size):>10}  '
      f'{total_objs:>9}  ({len(buckets)} buckets)')
" 2>/dev/null
fi

# ======================================================================
hdr "OBJECT BUCKET CLAIMS"
# ======================================================================
OBC_DATA=$(oc get obc -A \
	-o jsonpath='{range .items[*]}{.metadata.namespace}{"|"}{.metadata.name}{"|"}{.spec.storageClassName}{"\n"}{end}' 2>/dev/null || true)
if [[ -n "${OBC_DATA}" ]]; then
	printf "  %-30s  %-45s  %s\n" "NAMESPACE/OBC" "BUCKET" "STORAGE CLASS"
	echo "  $(printf '%0.s-' {1..95})"
	echo "${OBC_DATA}" | \
	while IFS='|' read -r ns name sc; do
		[[ -z "${ns}" ]] && continue
		bucket=$(oc get configmap "${name}" -n "${ns}" \
			-o jsonpath='{.data.BUCKET_NAME}' 2>/dev/null || echo "?")
		printf "  %-30s  %-45s  %s\n" "${ns}/${name}" "${bucket}" "${sc}"
	done
else
	info "No ObjectBucketClaims found."
fi

# ======================================================================
# QUAY SECTION — only if Quay is deployed
# ======================================================================
# Auto-detect Quay namespace: "quay" on hubs, "quay-enterprise" on pops
QUAY_NS=""
for qns in quay quay-enterprise; do
	if oc get namespace "${qns}" &>/dev/null; then
		QUAY_NS="${qns}"
		break
	fi
done
if [[ -n "${QUAY_NS}" ]]; then

	hdr "PVC INVENTORY (${QUAY_NS} namespace)"
	oc get pvc -n "${QUAY_NS}" --no-headers -o custom-columns=\
'NAME:.metadata.name,CAPACITY:.status.capacity.storage,SC:.spec.storageClassName,STATUS:.status.phase' \
		2>/dev/null | sort -k2 -h

	hdr "QUAY — PER-ORG STORAGE BREAKDOWN (${QUAY_NS})"

	# Discover DB pod — Quay 3.15 uses label quay-component=postgres,
	# Quay 3.16 may use quay-component=quay-database or postgres.
	DB_POD=""
	for label in quay-component=postgres quay-component=quay-database; do
		candidate=$(oc get pods -n "${QUAY_NS}" -l "${label}" \
			-o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
		if [[ -n "${candidate}" ]]; then
			DB_POD="${candidate}"
			break
		fi
	done

	if [[ -z "${DB_POD}" ]]; then
		warn "No Quay database pod found in namespace '${QUAY_NS}'. Skipping Quay breakdown."
	else
		QUAY_CR=$(oc get quayregistry -n "${QUAY_NS}" \
			-o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
		QUAY_VER=$(oc get quayregistry -n "${QUAY_NS}" \
			-o jsonpath='{.items[0].status.currentVersion}' 2>/dev/null || echo "unknown")
		QUAY_DB="${QUAY_CR:-regional-quay}-quay-database"
		info "Quay version: ${QUAY_VER}  DB pod: ${DB_POD}  DB name: ${QUAY_DB}"

		# Validate DB connectivity (read-only query)
		if ! oc exec "${DB_POD}" -n "${QUAY_NS}" -- \
			psql -U postgres -d "${QUAY_DB}" -c "SELECT 1;" &>/dev/null; then
			warn "Cannot query database ${QUAY_DB}. Skipping Quay breakdown."
		else
			echo ""
			echo "  Per-org storage (from quotanamespacesize):"
			echo "  $(printf '%0.s-' {1..60})"
			oc exec "${DB_POD}" -n "${QUAY_NS}" -- psql -U postgres -d "${QUAY_DB}" \
				-P pager=off -t -A -F'|' -c "
				SELECT u.username,
				       pg_size_pretty(qns.size_bytes) AS consumed,
				       qns.size_bytes
				FROM quotanamespacesize qns
				JOIN \"user\" u ON qns.namespace_user_id = u.id
				WHERE qns.size_bytes > 0
				ORDER BY qns.size_bytes DESC;" 2>/dev/null | \
			while IFS='|' read -r org size bytes; do
				printf "  %-30s  %12s\n" "${org}" "${size}"
			done

			echo ""
			echo "  Per-org detail (repos, manifests, blob storage):"
			echo "  $(printf '%0.s-' {1..80})"
			printf "  %-20s  %6s  %8s  %12s\n" "ORG" "REPOS" "MANIFESTS" "BLOB SIZE"
			echo "  $(printf '%0.s-' {1..80})"
			oc exec "${DB_POD}" -n "${QUAY_NS}" -- psql -U postgres -d "${QUAY_DB}" \
				-P pager=off -t -A -F'|' -c "
				SELECT u.username,
				       count(DISTINCT r.id) AS repos,
				       count(DISTINCT m.id) AS manifests,
				       pg_size_pretty(coalesce(sum(DISTINCT s.image_size), 0)) AS blob_size,
				       coalesce(sum(DISTINCT s.image_size), 0) AS raw_bytes
				FROM repository r
				JOIN \"user\" u ON r.namespace_user_id = u.id
				LEFT JOIN manifest m ON m.repository_id = r.id
				LEFT JOIN manifestblob mb ON mb.manifest_id = m.id
				LEFT JOIN imagestorage s ON s.id = mb.blob_id
				GROUP BY u.username
				ORDER BY coalesce(sum(DISTINCT s.image_size), 0) DESC;" 2>/dev/null | \
			while IFS='|' read -r org repos manifests size raw; do
				printf "  %-20s  %6s  %8s  %12s\n" "${org}" "${repos}" "${manifests}" "${size}"
			done

			echo ""
			echo "  Top 10 largest repositories:"
			echo "  $(printf '%0.s-' {1..100})"
			printf "  %-64s  %-15s  %12s\n" "REPOSITORY" "ORG" "SIZE"
			echo "  $(printf '%0.s-' {1..100})"
			oc exec "${DB_POD}" -n "${QUAY_NS}" -- psql -U postgres -d "${QUAY_DB}" \
				-P pager=off -t -A -F'|' -c "
				SELECT u.username || '/' || r.name AS fullname,
				       u.username AS org,
				       pg_size_pretty(coalesce(sum(DISTINCT s.image_size), 0)) AS repo_size,
				       coalesce(sum(DISTINCT s.image_size), 0) AS raw_bytes
				FROM repository r
				JOIN \"user\" u ON r.namespace_user_id = u.id
				LEFT JOIN manifest m ON m.repository_id = r.id
				LEFT JOIN manifestblob mb ON mb.manifest_id = m.id
				LEFT JOIN imagestorage s ON s.id = mb.blob_id
				GROUP BY u.username, r.name
				ORDER BY coalesce(sum(DISTINCT s.image_size), 0) DESC
				LIMIT 10;" 2>/dev/null | \
			while IFS='|' read -r fullname org size raw; do
				printf "  %-64s  %-15s  %12s\n" "${fullname}" "${org}" "${size}"
			done
		fi
	fi
else
	info "Quay namespace not found. Skipping Quay breakdown."
fi

# ======================================================================
hdr "CEPH POOL TOTALS"
# ======================================================================
if ${CEPH_TOOLS}; then
	RGW_POOL="${SC_NAME}-cephobjectstore.rgw.buckets.data"
	BLOCK_POOL="${SC_NAME}-cephblockpool"
	RGW_POOL_BYTES=$(ceph_cmd rados df -p "${RGW_POOL}" 2>/dev/null | \
		awk -v p="${RGW_POOL}" '$0 ~ p {print $2}')
	RGW_POOL_UNIT=$(ceph_cmd rados df -p "${RGW_POOL}" 2>/dev/null | \
		awk -v p="${RGW_POOL}" '$0 ~ p {print $3}')
	BLOCK_USED=$(ceph_cmd rados df -p "${BLOCK_POOL}" 2>/dev/null | \
		awk -v p="${BLOCK_POOL}" '$0 ~ p {print $2, $3}')
	RGW_REPLICA=$(ceph_cmd ceph osd pool get "${RGW_POOL}" size 2>/dev/null | awk '{print $2}')
	RGW_REPLICA=${RGW_REPLICA:-3}

	echo "  Ceph RGW pool (raw, ${RGW_REPLICA}x replicated):  ${RGW_POOL_BYTES:-?} ${RGW_POOL_UNIT}"

	if [[ -n "${DB_POD}" && -n "${QUAY_DB}" ]]; then
		QUAY_LOGICAL=$(oc exec "${DB_POD}" -n "${QUAY_NS}" -- psql -U postgres -d "${QUAY_DB}" \
			-t -c "SELECT coalesce(sum(size_bytes),0) FROM quotanamespacesize;" 2>/dev/null | tr -d ' ')
		if [[ -n "${QUAY_LOGICAL}" && "${QUAY_LOGICAL}" -gt 0 ]] 2>/dev/null; then
			python3 -c "
rgw_raw_str = '${RGW_POOL_BYTES} ${RGW_POOL_UNIT}'.strip()
quay_logical = ${QUAY_LOGICAL}
replica = ${RGW_REPLICA}

units = {'B':1, 'KiB':1024, 'MiB':1024**2, 'GiB':1024**3, 'TiB':1024**4}
parts = rgw_raw_str.split()
if len(parts) == 2:
    rgw_raw = float(parts[0]) * units.get(parts[1], 1)
else:
    rgw_raw = 0

def pretty(b):
    if b >= 1024**4: return f'{b/1024**4:.1f} TiB'
    if b >= 1024**3: return f'{b/1024**3:.1f} GiB'
    if b >= 1024**2: return f'{b/1024**2:.0f} MiB'
    return f'{b/1024:.0f} KiB'

quay_raw = quay_logical * replica
other_raw = max(rgw_raw - quay_raw, 0)
other_logical = other_raw / replica
print(f'  ├─ Quay registry (logical):          {pretty(quay_logical)}  (~{pretty(quay_raw)} raw)')
print(f'  └─ Other (ACM metrics, etcd, etc.):  ~{pretty(other_logical)}  (~{pretty(other_raw)} raw)')
" 2>/dev/null
		fi
	fi

	echo "  Ceph block pool (all RBD PVCs):      ${BLOCK_USED:-unknown}"
else
	info "Ceph tools pod not available. Enable it for pool totals."
fi

fi # HAS_STORAGECLUSTER

# ======================================================================
# ACM SECTION — only if ACM Observability is deployed
# ======================================================================
ACM_NS="open-cluster-management-observability"
if oc get namespace "${ACM_NS}" &>/dev/null; then

	hdr "ACM OBSERVABILITY — SPOKE CLUSTER METRICS STORAGE"

	# Managed clusters (use jsonpath for reliable column parsing)
	echo "  Managed clusters:"
	echo "  $(printf '%0.s-' {1..80})"
	printf "  %-35s  %-12s  %-12s  %s\n" "CLUSTER" "AVAILABLE" "JOINED" "AGE"
	echo "  $(printf '%0.s-' {1..80})"
	oc get managedclusters -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{range .status.conditions[?(@.type=="ManagedClusterConditionAvailable")]}{.status}{end}{"|"}{range .status.conditions[?(@.type=="ManagedClusterJoined")]}{.status}{end}{"|"}{.metadata.creationTimestamp}{"\n"}{end}' 2>/dev/null | \
	while IFS='|' read -r name avail joined created; do
		# Compute age from creationTimestamp
		if [[ -n "${created}" ]]; then
			created_epoch=$(date -d "${created}" +%s 2>/dev/null || echo "")
			if [[ -n "${created_epoch}" ]]; then
				now_epoch=$(date +%s)
				age_days=$(( (now_epoch - created_epoch) / 86400 ))
				age="${age_days}d"
			else
				age="?"
			fi
		else
			age="?"
		fi
		printf "  %-35s  %-12s  %-12s  %s\n" "${name}" "${avail:-Unknown}" "${joined:-Unknown}" "${age}"
	done

	echo ""
	echo "  Observability PVC allocations and actual usage:"
	echo "  $(printf '%0.s-' {1..80})"
	printf "  %-55s  %6s  %6s  %5s\n" "PVC" "ALLOC" "USED" "USE%"
	echo "  $(printf '%0.s-' {1..80})"

	# Cache pod list once (avoid repeated API calls)
	ACM_PODS_JSON=$(oc get pods -n "${ACM_NS}" -o json 2>/dev/null)

	for pvc_line in $(oc get pvc -n "${ACM_NS}" --no-headers 2>/dev/null | awk '{print $1 "|" $4}'); do
		pvc_name=$(echo "${pvc_line}" | cut -d'|' -f1)
		pvc_size=$(oc get pvc "${pvc_name}" -n "${ACM_NS}" -o jsonpath='{.spec.resources.requests.storage}' 2>/dev/null)

		# Find the pod mounting this PVC
		mount_pod=$(echo "${ACM_PODS_JSON}" | python3 -c "
import json, sys
pods = json.load(sys.stdin)
for p in pods.get('items', []):
    for v in p.get('spec', {}).get('volumes', []):
        pvc = v.get('persistentVolumeClaim', {}).get('claimName', '')
        if pvc == '${pvc_name}':
            print(p['metadata']['name'])
            sys.exit(0)
" 2>/dev/null || true)

		used="" pct=""
		if [[ -n "${mount_pod}" ]]; then
			for mpath in /var/thanos/receive /var/thanos/compact /var/thanos/store /var/thanos/rule /alertmanager; do
				df_out=$(oc exec "${mount_pod}" -n "${ACM_NS}" -- df -h "${mpath}" 2>/dev/null | tail -1)
				if [[ -n "${df_out}" ]]; then
					used=$(echo "${df_out}" | awk '{print $3}')
					pct=$(echo "${df_out}" | awk '{print $5}')
					break
				fi
			done
		fi
		printf "  %-55s  %6s  %6s  %5s\n" "${pvc_name}" "${pvc_size}" "${used:-?}" "${pct:-?}"
	done

	# Subtotals by component (alertmanager, thanos)
	echo "  $(printf '%0.s-' {1..80})"
	oc get pvc -n "${ACM_NS}" --no-headers 2>/dev/null | awk '{print $1}' | while read -r pvc_name; do
		pvc_bytes=$(oc get pvc "${pvc_name}" -n "${ACM_NS}" \
			-o jsonpath='{.spec.resources.requests.storage}' 2>/dev/null)
		echo "${pvc_name}|${pvc_bytes}"
	done | python3 -c "
import sys, re

def to_bytes(s):
    s = s.strip()
    m = re.match(r'^(\d+)(Gi|Mi|Ki|Ti)?$', s)
    if not m: return 0
    n = int(m.group(1))
    u = m.group(2) or ''
    return n * {'Ti':1<<40,'Gi':1<<30,'Mi':1<<20,'Ki':1<<10,'':1}.get(u,1)

def pretty(b):
    if b >= 1<<40: return f'{b/(1<<40):.1f} TiB'
    if b >= 1<<30: return f'{b/(1<<30):.1f} GiB'
    if b >= 1<<20: return f'{b/(1<<20):.0f} MiB'
    return f'{b/(1<<10):.0f} KiB'

totals = {}
for line in sys.stdin:
    name, size = line.strip().split('|',1)
    if 'alertmanager' in name: grp = 'Alertmanager'
    elif 'thanos' in name: grp = 'Thanos'
    else: grp = 'Other'
    b = to_bytes(size)
    totals.setdefault(grp, [0,0])
    totals[grp][0] += 1
    totals[grp][1] += b

grand = sum(v[1] for v in totals.values())
for grp in ['Alertmanager','Thanos','Other']:
    if grp in totals:
        cnt, b = totals[grp]
        print(f'  {grp:<20s}  {cnt} PVCs  allocated {pretty(b)}')
print(f'  {\"TOTAL\":<20s}  {sum(v[0] for v in totals.values())} PVCs  allocated {pretty(grand)}')
" 2>/dev/null

else
	info "ACM Observability not deployed. Skipping spoke metrics section."
fi

# ======================================================================
hdr "SUMMARY"
# ======================================================================
SC_COUNT=$(echo "${SC_JSON}" | python3 -c "import json,sys; print(json.load(sys.stdin)['spec']['storageDeviceSets'][0]['count'])" 2>/dev/null)
echo "  Cluster:          ${HUB_SHORT}"

if ${CEPH_TOOLS}; then
	CEPH_HEALTH=$(ceph_cmd ceph health | awk '{print $1}' 2>/dev/null)
	CEPH_DF=$(ceph_cmd ceph df 2>/dev/null)
	TOTAL_RAW=$(echo "${CEPH_DF}" | awk '/TOTAL/{print $2, $3}')
	TOTAL_AVAIL=$(echo "${CEPH_DF}" | awk '/TOTAL/{print $4, $5}')
	TOTAL_USED=$(echo "${CEPH_DF}" | awk '/TOTAL/{print $6, $7}')
	PCT=$(echo "${CEPH_DF}" | awk '/TOTAL/{print $NF}')
	OSD_COUNT=$(ceph_cmd ceph osd stat 2>/dev/null | awk '{print $1}')
	echo "  Health:           ${CEPH_HEALTH}"
	echo "  OSDs:             ${OSD_COUNT} (count=${SC_COUNT:-?} x replica=3)"
	echo "  Raw capacity:     ${TOTAL_RAW}"
	echo "  Used:             ${TOTAL_USED} (${PCT}%)"
	echo "  Available:        ${TOTAL_AVAIL}"
elif ${HAS_STORAGECLUSTER}; then
	echo "  Ceph tools:       not available (enable for capacity details)"
else
	echo "  ODF:              not deployed"
fi

if [[ -n "${DB_POD}" ]]; then
	QUAY_BYTES=$(oc exec "${DB_POD}" -n "${QUAY_NS}" -- psql -U postgres -d "${QUAY_DB}" \
		-t -c "SELECT coalesce(sum(size_bytes),0) FROM quotanamespacesize;" 2>/dev/null | tr -d ' ')
	if [[ -n "${QUAY_BYTES}" && "${QUAY_BYTES}" -gt 0 ]] 2>/dev/null; then
		QUAY_PRETTY=$(awk "BEGIN {
			b=${QUAY_BYTES};
			if (b >= 1099511627776) printf \"%.2f TiB\", b/1099511627776;
			else if (b >= 1073741824) printf \"%.1f GiB\", b/1073741824;
			else if (b >= 1048576) printf \"%.0f MiB\", b/1048576;
			else printf \"%.0f KiB\", b/1024;
		}")
		echo "  Quay total:       ${QUAY_PRETTY}"
	fi
fi

echo ""
info "Report generated at $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
