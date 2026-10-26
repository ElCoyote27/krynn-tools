#!/bin/bash

# Reversal of ocp_disconnected_optouts.sh
# Re-enables upstream updates, Insights, Telemeter,
# and CDI boot image imports.

###############################################################
# 1) Insights — restore cloud.openshift.com in pull-secret
#    Derives it from the existing quay.io entry (same creds).
###############################################################
echo "Re-enabling Insights (cloud.openshift.com)..."
oc extract secret/pull-secret -n openshift-config \
  --to=/var/tmp --confirm
if jq -e '.auths."quay.io"' /var/tmp/.dockerconfigjson >/dev/null 2>&1; then
  jq '.auths."cloud.openshift.com" = .auths."quay.io"' \
    /var/tmp/.dockerconfigjson > /var/tmp/.dockerconfigjson-upd
  oc set data secret/pull-secret -n openshift-config \
    --from-file=.dockerconfigjson=/var/tmp/.dockerconfigjson-upd
else
  echo "  SKIP: quay.io entry not found in pull-secret."
fi
rm -f /var/tmp/.dockerconfigjson /var/tmp/.dockerconfigjson-upd

###############################################################
# 2) Re-enable upstream update graph
###############################################################
echo "Re-enabling upstream update graph..."
oc patch clusterversion/version --type=merge \
  --patch '{"spec":{"upstream":"https://api.openshift.com/api/upgrades_info/v1/graph"}}'

###############################################################
# 3) Re-enable Telemeter client
###############################################################
echo "Re-enabling Telemeter client..."
if oc get cm -n openshift-monitoring cluster-monitoring-config >/dev/null 2>&1; then
  oc get cm -n openshift-monitoring cluster-monitoring-config \
    -o jsonpath='{.data.config\.yaml}' | \
    sed -E '/^telemeterClient:$/ {N; /\n[[:space:]]{2,}enabled:/ d; }' \
    > /var/tmp/cluster-monitoring-config.yaml
  printf '\ntelemeterClient:\n  enabled: true\n' \
    >> /var/tmp/cluster-monitoring-config.yaml
  jq -n --arg cfg "$(cat /var/tmp/cluster-monitoring-config.yaml)" \
    '{data:{"config.yaml":$cfg}}' \
    > /var/tmp/cluster-monitoring-config-patch.json
  oc patch cm -n openshift-monitoring cluster-monitoring-config \
    --type=merge \
    --patch-file /var/tmp/cluster-monitoring-config-patch.json
  rm -f /var/tmp/cluster-monitoring-config.yaml \
        /var/tmp/cluster-monitoring-config-patch.json
else
  echo "  SKIP: cluster-monitoring-config not found (Telemeter enabled by default)."
fi

###############################################################
# 4) Re-enable OCP-V CDI common boot image import
###############################################################
echo "Re-enabling CDI common boot image import..."
oc patch hyperconverged kubevirt-hyperconverged -n openshift-cnv \
  --type json \
  -p '[{"op": "replace", "path": "/spec/featureGates/enableCommonBootImageImport", "value": true}]' \
  2>/dev/null || echo "  SKIP: HyperConverged CR not found (OCP-V not installed?)"

echo "Done."
