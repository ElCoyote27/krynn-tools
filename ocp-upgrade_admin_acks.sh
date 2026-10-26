#!/bin/bash

# Pre-populate every known OpenShift admin acknowledgement (ACK)
# so that upgrades between minor versions are never blocked by
# the admin-ack gate.
#
# The admin-ack mechanism was introduced in OCP 4.8.14 (Aug 2021).
# The CVO only checks gates matching the *current* cluster version,
# so pre-setting future ACKs is completely harmless — they sit in the
# ConfigMap until the cluster reaches that version.
#
# Usage:
#   ./ocp_upgrade_admin_acks.sh                 # all ACKs from 4.8 → latest known
#   ./ocp_upgrade_admin_acks.sh <from> <to>     # ACKs for 4.<from> → 4.<to>
#
# Examples:
#   ./ocp_upgrade_admin_acks.sh                 # 4.8 → 4.22 (everything)
#   ./ocp_upgrade_admin_acks.sh 14 18           # 4.14 → 4.18
#   ./ocp_upgrade_admin_acks.sh 18 22           # 4.18 → 4.22
#
# Sources:
#   https://access.redhat.com/articles/6955985
#   https://docs.redhat.com/en/documentation/openshift_container_platform/

set -euo pipefail

# ── ACK registry ────────────────────────────────────────────────
# Maps the TARGET minor version to its admin-ack key.
# Only versions that actually gate on an ACK are listed;
# versions with no gate are intentionally absent.
#
# Upgrade       Kube    ACK key
# ───────────   ─────   ─────────────────────────────────────────────────────────
# 4.8  → 4.9   1.22    ack-4.8-kube-1.22-api-removals-in-4.9
# 4.9  → 4.10  1.23    (none — no API removals)
# 4.10 → 4.11  1.24    (none — no API removals)
# 4.11 → 4.12  1.25    ack-4.11-kube-1.25-api-removals-in-4.12
# 4.12 → 4.13  1.26    ack-4.12-kube-1.26-api-removals-in-4.13
# 4.13 → 4.14  1.27    ack-4.13-kube-1.27-api-removals-in-4.14
# 4.14 → 4.15  1.28    (none — no API removals)
# 4.15 → 4.16  1.29    ack-4.15-kube-1.29-api-removals-in-4.16
# 4.16 → 4.17  1.30    (none — no API removals)
# 4.17 → 4.18  1.31    (none — no API removals)
# 4.18 → 4.19  1.32    ack-4.18-kube-1.32-api-removals-in-4.19
# 4.19 → 4.20  1.33    ack-4.19-admissionregistration-v1beta1-api-removals-in-4.20
# 4.20 → 4.21  1.34    ack-4.20-sigstore-in-4.21  (Sigstore sigs for mirrored registries)
# 4.21 → 4.22  1.35    ack-4.21-boot-image-opt-out-in-4.22  (Azure/vSphere boot image change)
# 4.22 → 4.23  -       (none known yet)
#
# NOTE: Not all ACKs are about Kubernetes API removals:
#   4.21 — Sigstore signature verification for mirrored/disconnected registries
#   4.22 — boot-image default change on Azure/vSphere (harmless elsewhere)
#   These are safe to pre-populate on all platforms.
#
# ⚠  When Red Hat publishes new ACKs, add them here.

declare -A ACKS
ACKS[9]="ack-4.8-kube-1.22-api-removals-in-4.9"
ACKS[12]="ack-4.11-kube-1.25-api-removals-in-4.12"
ACKS[13]="ack-4.12-kube-1.26-api-removals-in-4.13"
ACKS[14]="ack-4.13-kube-1.27-api-removals-in-4.14"
ACKS[16]="ack-4.15-kube-1.29-api-removals-in-4.16"
ACKS[19]="ack-4.18-kube-1.32-api-removals-in-4.19"
ACKS[20]="ack-4.19-admissionregistration-v1beta1-api-removals-in-4.20"
ACKS[21]="ack-4.20-sigstore-in-4.21"
ACKS[22]="ack-4.21-boot-image-opt-out-in-4.22"

declare -A NOTES
NOTES[9]="Kube 1.22 — many v1beta1 APIs removed (CRDs, webhooks, etc.)"
NOTES[12]="Kube 1.25 — CronJob batch/v1beta1, EndpointSlice discovery/v1beta1, etc."
NOTES[13]="Kube 1.26 — FlowSchema/PriorityLevelConfig flowcontrol/v1beta1"
NOTES[14]="Kube 1.27 — CSIStorageCapacity storage/v1beta1"
NOTES[16]="Kube 1.29 — FlowSchema/PriorityLevelConfig flowcontrol/v1beta2"
NOTES[19]="Kube 1.32 — FlowSchema/PriorityLevelConfig flowcontrol/v1beta3"
NOTES[20]="admissionregistration.k8s.io/v1beta1 re-removed (reintroduced in 4.17)"
NOTES[21]="Sigstore signatures required for mirrored registries"
NOTES[22]="Boot image default change on Azure/vSphere (harmless elsewhere)"

# Highest minor version we have data for
MAX_KNOWN=22

# ── Defaults / argument handling ────────────────────────────────
FROM=${1:-8}
TO=${2:-${MAX_KNOWN}}

usage() {
  cat <<EOF
Usage: $0 [<current_minor> <target_minor>]

  <current_minor>  Current OCP 4.x minor version (default: 8)
  <target_minor>   Target  OCP 4.x minor version (default: ${MAX_KNOWN})

Examples:
  $0              # pre-populate everything: 4.8 → 4.${MAX_KNOWN}
  $0 14 18        # ACKs for 4.14 → 4.18
  $0 18 22        # ACKs for 4.18 → 4.22
EOF
  exit 1
}

[[ "${FROM}" == "-h" || "${FROM}" == "--help" ]] && usage

if ! [[ "$FROM" =~ ^[0-9]+$ && "$TO" =~ ^[0-9]+$ ]]; then
  echo "ERROR: arguments must be integers (minor version numbers)." >&2
  usage
fi

if (( TO <= FROM )); then
  echo "ERROR: target (4.${TO}) must be greater than current (4.${FROM})." >&2
  exit 1
fi

if (( FROM < 8 )); then
  echo "NOTE: admin-ack mechanism was introduced in 4.8.14; starting from 4.8."
  FROM=8
fi

if (( TO > MAX_KNOWN )); then
  echo "WARNING: this script only knows ACKs up to 4.${MAX_KNOWN}."
  echo "         4.$((MAX_KNOWN+1))–4.${TO} may have new ACKs — update the script!"
  echo ""
fi

# ── Preflight ───────────────────────────────────────────────────
echo "============================================================"
echo " OCP Admin ACKs: 4.${FROM} → 4.${TO}"
echo "============================================================"

if ! oc whoami &>/dev/null; then
  echo "ERROR: not logged in to an OpenShift cluster (oc whoami failed)." >&2
  exit 1
fi
echo "Cluster: $(oc whoami --show-server)"
echo "User:    $(oc whoami)"
echo ""

# Ensure the admin-acks ConfigMap exists
if ! oc -n openshift-config get cm admin-acks &>/dev/null; then
  echo "Creating admin-acks ConfigMap (does not exist yet)..."
  oc -n openshift-config create cm admin-acks
  echo ""
fi

# ── Build a single patch with every applicable ACK ──────────────
patch='{"data":{'
applied=0
skipped=0
cap=$(( TO < MAX_KNOWN ? TO : MAX_KNOWN ))

for (( v = FROM + 1; v <= cap; v++ )); do
  key="${ACKS[$v]:-}"
  if [[ -z "$key" ]]; then
    printf "  4.%-2d  ✓ no admin ACK required\n" "$v"
    (( skipped++ )) || true
    continue
  fi

  note="${NOTES[$v]:-}"
  printf "  4.%-2d  ⚙ %s\n" "$v" "$note"
  printf "        → %s\n" "$key"

  (( applied > 0 )) && patch+=','
  patch+="\"${key}\":\"true\""
  (( applied++ )) || true
done

patch+='}}'

echo ""

# ── Apply in one shot ───────────────────────────────────────────
if (( applied > 0 )); then
  echo "Applying ${applied} ACK(s) in a single patch..."
  oc -n openshift-config patch cm admin-acks \
    --patch "${patch}" \
    --type=merge
fi

echo ""
echo "------------------------------------------------------------"
echo " Done.  Applied: ${applied}  |  Skipped (no ACK needed): ${skipped}"
echo "------------------------------------------------------------"
if (( TO > MAX_KNOWN )); then
  echo ""
  echo " ⚠  Versions beyond 4.${MAX_KNOWN} were not covered."
  echo "    Check https://access.redhat.com/articles/6955985"
  echo "    for new ACKs and update this script."
fi
