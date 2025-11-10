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
