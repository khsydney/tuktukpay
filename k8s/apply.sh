#!/usr/bin/env bash
# Apply the TukTukPay manifests to the current kube-context.
#   IMAGE_REGISTRY=<registry> NAMESPACE=tuktukpay-team01 DEPLOYMENT_ENVIRONMENT=tuktukpay-team01 k8s/apply.sh
# Defaults: images built by docker compose and loaded locally (kind), namespace tuktukpay, environment tuktukpay-k8s.
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
: "${IMAGE_REGISTRY:=tuktukpay-workshop}"
: "${NAMESPACE:=tuktukpay}"
: "${DEPLOYMENT_ENVIRONMENT:=tuktukpay-k8s}"
sed -e "s#\${IMAGE_REGISTRY}#${IMAGE_REGISTRY}#g" \
    -e "s#\${NAMESPACE}#${NAMESPACE}#g" \
    -e "s#\${DEPLOYMENT_ENVIRONMENT}#${DEPLOYMENT_ENVIRONMENT}#g" "$DIR/tuktukpay.yaml" | kubectl apply -f -
echo "applied namespace=$NAMESPACE environment=$DEPLOYMENT_ENVIRONMENT images=$IMAGE_REGISTRY/tuktukpay-*"
echo "watch:   kubectl -n $NAMESPACE get pods -w"
echo "chaos:   kubectl -n $NAMESPACE port-forward svc/chaos-controller 8090:8090"
