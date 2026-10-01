#!/usr/bin/env bash
# One-command local Kubernetes for the workshop (no EKS needed): kind cluster + Splunk OTel Collector
# Helm chart with the OpenTelemetry Operator + the TukTukPay stack, instrumented by pod annotations.
#
# Requirements: docker, kind, kubectl, helm. Reads SPLUNK_REALM / SPLUNK_ACCESS_TOKEN from ../.env.
#   k8s/kind-up.sh                      # cluster "tuktukpay", namespace tuktukpay, environment tuktukpay-k8s
#   NAMESPACE=tuktukpay-team01 DEPLOYMENT_ENVIRONMENT=tuktukpay-team01 k8s/kind-up.sh   # per-team namespace on the same cluster
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$DIR/.." && pwd)
CLUSTER=${CLUSTER:-tuktukpay}
: "${NAMESPACE:=tuktukpay}"
: "${DEPLOYMENT_ENVIRONMENT:=tuktukpay-k8s}"
[ -f "$ROOT/.env" ] && set -a && . "$ROOT/.env" && set +a
: "${SPLUNK_REALM:?set SPLUNK_REALM in .env}"
: "${SPLUNK_ACCESS_TOKEN:?set SPLUNK_ACCESS_TOKEN in .env}"

SERVICES="chaos-controller checkout-api risk-engine payment-router acquirer-sim ledger webhook-dispatcher merchant-copilot wallet-sim loadgen"

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "== creating kind cluster $CLUSTER"
  kind create cluster --name "$CLUSTER" --wait 120s
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null

echo "== building images with docker compose"
( cd "$ROOT" && docker compose build $SERVICES )
echo "== loading images into kind"
for s in $SERVICES; do
  docker tag "tuktukpay-workshop-$s:latest" "tuktukpay-workshop/tuktukpay-$s:latest"
  kind load docker-image "tuktukpay-workshop/tuktukpay-$s:latest" --name "$CLUSTER"
done

echo "== installing the Splunk OTel Collector chart + OpenTelemetry Operator"
helm repo add splunk-otel-collector-chart https://signalfx.github.io/splunk-otel-collector-chart >/dev/null 2>&1 || true
helm repo update >/dev/null
helm upgrade --install splunk-otel-collector splunk-otel-collector-chart/splunk-otel-collector \
  --namespace splunk-otel --create-namespace \
  --set splunkObservability.accessToken="$SPLUNK_ACCESS_TOKEN" \
  --set splunkObservability.realm="$SPLUNK_REALM" \
  --set clusterName="kind-$CLUSTER" \
  -f "$DIR/values-splunk-otel-collector.yaml" --wait --timeout 5m
kubectl -n splunk-otel wait --for=condition=available deployment -l app.kubernetes.io/name=opentelemetry-operator --timeout=180s 2>/dev/null || true

echo "== deploying TukTukPay ($NAMESPACE / $DEPLOYMENT_ENVIRONMENT)"
IMAGE_REGISTRY=tuktukpay-workshop NAMESPACE="$NAMESPACE" DEPLOYMENT_ENVIRONMENT="$DEPLOYMENT_ENVIRONMENT" "$DIR/apply.sh"
kubectl -n "$NAMESPACE" rollout status deployment --timeout=300s 2>/dev/null || kubectl -n "$NAMESPACE" get pods
echo
echo "chaos panel:  kubectl -n $NAMESPACE port-forward svc/chaos-controller 8090:8090   -> http://localhost:8090"
echo "payments API: kubectl -n $NAMESPACE port-forward svc/checkout-api 8080:8080       -> scripts/smoke-test.sh"
echo "copilot:      kubectl -n $NAMESPACE port-forward svc/merchant-copilot 8086:8086"
echo "tear down:    kind delete cluster --name $CLUSTER"
