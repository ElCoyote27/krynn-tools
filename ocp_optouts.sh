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

# Set upstream to /openshift-release.apps.ci.l2s4.p1.openshiftapps.com
# oc patch clusterversion/version --patch '{"spec":{"upstream":"https://openshift-release.apps.ci.l2s4.p1.openshiftapps.com/graph"}}' --type=merge
