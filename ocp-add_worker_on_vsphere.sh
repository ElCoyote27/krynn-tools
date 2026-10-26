#!/bin/bash
#
# Add ONE worker to an OCP 4.18 vSphere IPI hub cluster with a static IP.
#
# The installer.openshift.io/IPPool has no running IPAM controller post-install,
# so we manually create the IPAddress + bind the IPAddressClaim after scaling
# the MachineSet.
#
# Run the script multiple times to add more workers.
#
# Usage:
#   export KUBECONFIG=/path/to/kubeconfig
#   ./add-hub-worker.sh <ip>
#
# Example:
#   export KUBECONFIG=/var/tmp/DUMP-Unsorted/kubeconfig-mvp1
#   ./add-hub-worker.sh 198.19.224.57
#   ./add-hub-worker.sh 198.19.224.58
#
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
die()  { echo -e "${RED}ERROR: $*${NC}" >&2; exit 1; }
info() { echo -e "${CYAN}>>> $*${NC}"; }
ok()   { echo -e "${GREEN} OK: $*${NC}"; }
warn() { echo -e "${YELLOW}WARNING: $*${NC}"; }

NS="openshift-machine-api"
WAIT_TIMEOUT=1800

# ── Arg parsing ──────────────────────────────────
IP="${1:-}"
[[ -n "$IP" ]] || die "Usage: $0 <ip-address>\n  Example: $0 198.19.224.57"
[[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Invalid IP format: $IP"

# ── Pre-flight: KUBECONFIG & cluster ─────────────
info "Pre-flight checks"

command -v oc &>/dev/null   || die "oc CLI not found in PATH"
[[ -n "${KUBECONFIG:-}" ]] || die "KUBECONFIG is not set.\n  export KUBECONFIG=/path/to/kubeconfig"
[[ -f "$KUBECONFIG" ]]     || die "KUBECONFIG file not found: $KUBECONFIG"
oc whoami &>/dev/null       || die "Cannot connect to cluster. Check KUBECONFIG=$KUBECONFIG"

CLUSTER_NAME=$(oc get infrastructure cluster -o jsonpath='{.status.infrastructureName}' 2>/dev/null) \
  || die "Cannot read infrastructure resource — is this an OpenShift cluster?"
API_URL=$(oc whoami --show-server)
PLATFORM=$(oc get infrastructure cluster -o jsonpath='{.status.platform}')
OCP_VERSION=$(oc get clusterversion version -o jsonpath='{.status.desired.version}' 2>/dev/null || echo "unknown")

echo "  Cluster:  $CLUSTER_NAME"
echo "  API:      $API_URL"
echo "  OCP:      $OCP_VERSION"
echo "  Platform: $PLATFORM"
[[ "$PLATFORM" == "VSphere" ]] || die "This script is for vSphere IPI clusters (got: $PLATFORM)"

# ── Pre-flight: MachineSet ───────────────────────
MACHINESET=$(oc get machinesets.machine.openshift.io -n "$NS" -o jsonpath='{.items[0].metadata.name}') \
  || die "No MachineSet found"
[[ -n "$MACHINESET" ]] || die "No MachineSet found"

CURRENT_REPLICAS=$(oc get machineset.machine.openshift.io "$MACHINESET" -n "$NS" -o jsonpath='{.spec.replicas}')

HAS_POOL=$(oc get machineset.machine.openshift.io "$MACHINESET" -n "$NS" \
  -o jsonpath='{.spec.template.spec.providerSpec.value.network.devices[0].addressesFromPools[0].name}')
[[ "$HAS_POOL" == "default-0" ]] || die "MachineSet does not use static IP pool"

echo "  MachineSet: $MACHINESET (replicas: $CURRENT_REPLICAS)"

# ── Pre-flight: Pool parameters ──────────────────
GATEWAY=$(oc get ipaddresses.ipam.cluster.x-k8s.io -n "$NS" -o jsonpath='{.items[0].spec.gateway}')
PREFIX=$(oc get ipaddresses.ipam.cluster.x-k8s.io -n "$NS" -o jsonpath='{.items[0].spec.prefix}')
[[ -n "$GATEWAY" && -n "$PREFIX" ]] || die "Cannot determine gateway/prefix from existing IPAddress objects"

echo "  Pool:       gw=$GATEWAY /$PREFIX"

# ── Pre-flight: IP not already allocated ─────────
EXISTING=$(oc get ipaddresses.ipam.cluster.x-k8s.io -n "$NS" \
  -o jsonpath="{range .items[?(@.spec.address==\"$IP\")]}{.metadata.name}{end}" 2>/dev/null || true)
[[ -z "$EXISTING" ]] || die "IP $IP is already allocated in this cluster (as IPAddress: $EXISTING)"

# ── Pre-flight: IP not assigned to an existing node ─
EXISTING_NODE=$(oc get nodes -o wide --no-headers 2>/dev/null \
  | awk -v ip="$IP" '$6 == ip || $7 == ip {print $1}' || true)
[[ -z "$EXISTING_NODE" ]] || die "IP $IP is already assigned to node: $EXISTING_NODE"

# ── Pre-flight: IP not used by an existing Machine ──
EXISTING_MACHINE=$(oc get machines.machine.openshift.io -n "$NS" \
  -o jsonpath="{range .items[*]}{.metadata.name}{\" \"}{.status.addresses[*].address}{\"\n\"}{end}" 2>/dev/null \
  | awk -v ip="$IP" '{for(i=2;i<=NF;i++) if($i==ip) print $1}' || true)
[[ -z "$EXISTING_MACHINE" ]] || die "IP $IP is already used by Machine: $EXISTING_MACHINE"

# ── Pre-flight: IP not live on network ───────────
if ping -c1 -W1 "$IP" &>/dev/null; then
  die "IP $IP responds to ping — it is already in use on the network!"
fi

# ── Pre-flight: required secrets ─────────────────
oc get secret worker-user-data -n "$NS" &>/dev/null       || die "Secret worker-user-data not found"
oc get secret vsphere-cloud-credentials -n "$NS" &>/dev/null || die "Secret vsphere-cloud-credentials not found"

ok "All pre-flight checks passed"

# ── Confirmation ─────────────────────────────────
echo ""
echo "  Will add 1 worker to $CLUSTER_NAME with IP $IP"
echo ""
read -rp "Proceed? (yes/no): " CONFIRM
[[ "$CONFIRM" == "yes" ]] || die "Aborted"

# ── Step 1: Scale MachineSet ─────────────────────
TARGET_REPLICAS=$((CURRENT_REPLICAS + 1))
info "Step 1/3: Scaling MachineSet $MACHINESET to $TARGET_REPLICAS"
oc scale machineset.machine.openshift.io "$MACHINESET" --replicas="$TARGET_REPLICAS" -n "$NS" || die "Failed to scale"
ok "MachineSet scaled"

# ── Step 2: Wait for unbound IPAddressClaim ──────
info "Step 2/3: Waiting for new IPAddressClaim..."

CLAIM=""
for attempt in $(seq 1 60); do
  CLAIM=$(oc get ipaddressclaims.ipam.cluster.x-k8s.io -n "$NS" \
    -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.addressRef.name}{"\n"}{end}' 2>/dev/null \
    | awk 'NF==1 {print $1}' | head -1)
  [[ -n "$CLAIM" ]] && break
  echo -ne "\r  Waiting... (${attempt}/60)"
  sleep 2
done
echo ""
[[ -n "$CLAIM" ]] || die "Timed out waiting for IPAddressClaim.\n  Check: oc get ipaddressclaims -n $NS"

MACHINE_NAME=$(echo "$CLAIM" | sed 's/-claim-0-0$//')
ok "Claim appeared: $CLAIM (machine: $MACHINE_NAME)"

# ── Step 3: Bind the IP ──────────────────────────
info "Step 3/3: Binding IP $IP to claim $CLAIM"

oc apply -f - <<EOF || die "Failed to create IPAddress"
apiVersion: ipam.cluster.x-k8s.io/v1beta1
kind: IPAddress
metadata:
  name: ${CLAIM}
  namespace: ${NS}
spec:
  address: ${IP}
  claimRef:
    name: ${CLAIM}
  gateway: ${GATEWAY}
  poolRef:
    apiGroup: installer.openshift.io
    kind: IPPool
    name: default-0
  prefix: ${PREFIX}
EOF

oc patch ipaddressclaim "$CLAIM" -n "$NS" \
  --type=merge --subresource=status \
  -p "{\"status\":{\"addressRef\":{\"name\":\"${CLAIM}\"}}}" \
  || die "Failed to bind claim"

ok "IP bound"

# ── Wait for Machine Running ────────────────────
info "Waiting for VM provisioning..."

DEADLINE=$((SECONDS + WAIT_TIMEOUT))
while [[ $SECONDS -lt $DEADLINE ]]; do
  PHASE=$(oc get machines.machine.openshift.io "$MACHINE_NAME" -n "$NS" \
    -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
  echo -ne "\r  Machine $MACHINE_NAME: $PHASE  [$(( DEADLINE - SECONDS ))s left]    "
  [[ "$PHASE" == "Running" ]] && break
  sleep 10
done
echo ""
[[ "$PHASE" == "Running" ]] || die "Machine did not reach Running phase within timeout.\n  oc logs -n $NS deploy/machine-api-controllers -c machine-controller --tail=30"
ok "Machine is Running"

# ── Wait for Node Ready ─────────────────────────
info "Waiting for node to become Ready..."

DEADLINE=$((SECONDS + WAIT_TIMEOUT))
while [[ $SECONDS -lt $DEADLINE ]]; do
  NODE_READY=$(oc get node "$MACHINE_NAME" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "NotFound")
  echo -ne "\r  Node $MACHINE_NAME: $NODE_READY  [$(( DEADLINE - SECONDS ))s left]    "
  [[ "$NODE_READY" == "True" ]] && break
  sleep 10
done
echo ""
[[ "$NODE_READY" == "True" ]] || die "Node did not become Ready within timeout.\n  oc get nodes; oc get csr"
ok "Node is Ready"

# ── Final status ─────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
echo -e "${GREEN} SUCCESS${NC}"
echo "════════════════════════════════════════════════════════════"
echo ""
oc get node "$MACHINE_NAME" -o wide
echo ""
WORKER_COUNT=$(oc get nodes -l 'node-role.kubernetes.io/worker,!node-role.kubernetes.io/infra' --no-headers | wc -l)
INFRA_COUNT=$(oc get nodes -l 'node-role.kubernetes.io/infra' --no-headers | wc -l)
TOTAL_NODES=$(oc get nodes --no-headers | wc -l)
echo "  Non-infra workers: $WORKER_COUNT"
echo "  Infra workers:     $INFRA_COUNT"
echo "  Total nodes:       $TOTAL_NODES"
echo ""
