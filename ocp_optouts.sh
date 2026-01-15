#!/bin/bash

# Insights
oc extract secret/pull-secret -n openshift-config --to=. --confirm
jq 'del(.auths|."cloud.openshift.com")' < .dockerconfigjson > .dockerconfigjson-upd
mv -f .dockerconfigjson-upd .dockerconfigjson
oc set data secret/pull-secret -n openshift-config --from-file=.dockerconfigjson=.dockerconfigjson
rm -f .dockerconfigjson

# OCP-V CDI
oc patch hyperconverged kubevirt-hyperconverged -n openshift-cnv \
--type json -p '[{"op": "replace", "path": "/spec/featureGates/enableCommonBootImageImport", "value": false}]'

# Unset channel
oc patch clusterversion version --type=json -p='[{"op": "remove", "path": "/spec/channel"}]'

# oc patch clusterversion/version --patch '{"spec":{"upstream":"https://openshift-release.apps.ci.l2s4.p1.openshiftapps.com/graph"}}' --type=merge
# Set upstream to none for disconnected:
oc patch clusterversion/version --patch '{"spec":{"upstream":"none"}}' --type=merge

# Disable Telemeter client
oc get cm -n openshift-monitoring cluster-monitoring-config -o jsonpath='{.data.config\.yaml}' > /var/tmp/cluster-monitoring-config.yaml
printf '\n' >> /var/tmp/cluster-monitoring-config.yaml
cat >> /var/tmp/cluster-monitoring-config.yaml <<'EOF'
telemeterClient:
  enabled: false
EOF
jq -n --arg cfg "$(cat /var/tmp/cluster-monitoring-config.yaml)" \
  '{data:{"config.yaml":$cfg}}' > /var/tmp/cluster-monitoring-config-patch.json
oc patch cm -n openshift-monitoring cluster-monitoring-config --type=merge \
  --patch-file /var/tmp/cluster-monitoring-config-patch.json
