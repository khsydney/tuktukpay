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
  echo "== creating kind cluster $CLUSTER (UI/API ports published on localhost, see kind-config.yaml)"
  kind create cluster --name "$CLUSTER" --config "$DIR/kind-config.yaml" --wait 120s
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null

echo "== building images with docker compose"
( cd "$ROOT" && docker compose build $SERVICES )
echo "== loading images into kind"
# Remember which images actually changed on the node: the manifests use a fixed `latest` tag with
# imagePullPolicy IfNotPresent, so a rebuilt image never restarts a running pod by itself.
NODE="$CLUSTER-control-plane"
CHANGED=""
for s in $SERVICES; do
  img="tuktukpay-workshop/tuktukpay-$s:latest"
  before=$(docker exec "$NODE" crictl images -q "docker.io/$img" 2>/dev/null || true)
  docker tag "tuktukpay-workshop-$s:latest" "$img"
  kind load docker-image "$img" --name "$CLUSTER"
  after=$(docker exec "$NODE" crictl images -q "docker.io/$img" 2>/dev/null || true)
  if [ -n "$before" ] && [ "$before" != "$after" ]; then CHANGED="$CHANGED $s"; fi
done

echo "== installing the Splunk OTel Collector chart + OpenTelemetry Operator"
helm repo add splunk-otel-collector-chart https://signalfx.github.io/splunk-otel-collector-chart >/dev/null 2>&1 || true
helm repo update >/dev/null
# Logs -> Splunk Cloud Platform HEC (Log Observer Connect), same variables as Docker Compose (docs/logs-setup.md).
HEC_ARGS=()
if [ -n "${SPLUNK_HEC_URL:-}" ] && [ -n "${SPLUNK_HEC_TOKEN:-}" ]; then
  HEC_ARGS=(--set "splunkPlatform.endpoint=$SPLUNK_HEC_URL" --set "splunkPlatform.token=$SPLUNK_HEC_TOKEN" --set "splunkPlatform.index=${SPLUNK_HEC_INDEX:-tuktukpay}")
  echo "   logs -> $SPLUNK_HEC_URL (index ${SPLUNK_HEC_INDEX:-tuktukpay})"
else
  echo "   SPLUNK_HEC_URL/SPLUNK_HEC_TOKEN not set: logs stay in kubectl logs only"
fi
helm upgrade --install splunk-otel-collector splunk-otel-collector-chart/splunk-otel-collector \
  --namespace splunk-otel --create-namespace \
  --set splunkObservability.accessToken="$SPLUNK_ACCESS_TOKEN" \
  --set splunkObservability.realm="$SPLUNK_REALM" \
  --set clusterName="kind-$CLUSTER" \
  --set environment="$DEPLOYMENT_ENVIRONMENT" \
  "${HEC_ARGS[@]}" \
  -f "$DIR/values-splunk-otel-collector.yaml" --wait --timeout 5m
kubectl -n splunk-otel wait --for=condition=available deployment -l app.kubernetes.io/name=opentelemetry-operator --timeout=180s 2>/dev/null || true

echo "== deploying TukTukPay ($NAMESPACE / $DEPLOYMENT_ENVIRONMENT)"
IMAGE_REGISTRY=tuktukpay-workshop NAMESPACE="$NAMESPACE" DEPLOYMENT_ENVIRONMENT="$DEPLOYMENT_ENVIRONMENT" "$DIR/apply.sh"
if [ -n "$CHANGED" ]; then
  # Code changed: restart exactly the deployments whose image changed, so "edit, run kind-up.sh"
  # works for code the same way apply.sh makes it work for .env (chaos flags reset with the controller).
  echo "== restarting deployments whose image changed:$CHANGED"
  for s in $CHANGED; do
    # image -> deployment(s): loadgen runs as merchant-storefront; the wallet-sim image also runs the agent console
    case $s in loadgen) deps=merchant-storefront ;; wallet-sim) deps="wallet-sim shopping-agent" ;; *) deps=$s ;; esac
    for d in $deps; do kubectl -n "$NAMESPACE" rollout restart "deployment/$d" >/dev/null 2>&1 || true; done
  done
fi
kubectl -n "$NAMESPACE" rollout status deployment --timeout=300s 2>/dev/null || kubectl -n "$NAMESPACE" get pods
echo
echo "UIs + API:    http://localhost:8090 (war room), :8087 (agent console), :8080 (payments), :8086 (copilot) — direct via NodePorts"
echo "              (other clusters without the kind port mappings: k8s/port-forward.sh $NAMESPACE)"
echo "smoke test:   scripts/smoke-test.sh"
echo "K8s acts:     act6 / act7 from the panel; k8s/acts/oversize-rollout.sh, k8s/acts/bad-image.sh, k8s/acts/rollback.sh"
echo "tear down:    kind delete cluster --name $CLUSTER"
echo "stay awake:   make awake — a sleeping laptop (closed lid, idle timer) freezes the whole cluster; wake = DNS blips + maybe a new VPN IP for the HEC allow list"
