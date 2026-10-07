#!/usr/bin/env bash
# Apply the TukTukPay manifests to the current kube-context, with the application settings
# taken from .env so one file configures both Docker Compose and Kubernetes:
#   - ConfigMap tuktukpay-config: BASE_RPS, COPILOT_*, AGENT_*, LOG_LEVEL, AWS_REGION ...
#   - Secret tuktukpay-secrets:   AMP_SHARED_SECRET and the LLM provider keys
# Deployments read both through envFrom; when either changed, the application pods are
# rolled so the new values take effect (Postgres and Redis are left alone).
#   IMAGE_REGISTRY=<registry> NAMESPACE=tuktukpay-team01 DEPLOYMENT_ENVIRONMENT=tuktukpay-team01 k8s/apply.sh
# Defaults: images built by docker compose and loaded locally (kind), namespace tuktukpay, environment tuktukpay-k8s.
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$DIR/.." && pwd)
[ -f "$ROOT/.env" ] && set -a && . "$ROOT/.env" && set +a
: "${IMAGE_REGISTRY:=tuktukpay-workshop}"
: "${NAMESPACE:=tuktukpay}"
: "${DEPLOYMENT_ENVIRONMENT:=tuktukpay-k8s}"
: "${SERVICE_VERSION:=2026.09.1}"

sed -e "s#\${IMAGE_REGISTRY}#${IMAGE_REGISTRY}#g" \
    -e "s#\${NAMESPACE}#${NAMESPACE}#g" \
    -e "s#\${DEPLOYMENT_ENVIRONMENT}#${DEPLOYMENT_ENVIRONMENT}#g" \
    -e "s#\${SERVICE_VERSION}#${SERVICE_VERSION}#g" "$DIR/tuktukpay.yaml" | kubectl apply -f -

# Application settings (same names and defaults as docker-compose.yml / .env.example).
config_args=()
for kv in "BASE_RPS=${BASE_RPS:-4}" "COPILOT_INTERVAL_S=${COPILOT_INTERVAL_S:-8}" \
          "COPILOT_PROVIDER=${COPILOT_PROVIDER:-mock}" "COPILOT_MODEL=${COPILOT_MODEL:-}" \
          "COPILOT_GUARDRAIL=${COPILOT_GUARDRAIL:-on}" "COPILOT_CAPTURE_CONTENT=${COPILOT_CAPTURE_CONTENT:-false}" \
          "AGENT_PROVIDER=${AGENT_PROVIDER:-mock}" "AGENT_MODEL=${AGENT_MODEL:-}" "AGENT_GUARDRAIL=${AGENT_GUARDRAIL:-flag}" \
          "LOG_LEVEL=${LOG_LEVEL:-info}" "AWS_REGION=${AWS_REGION:-ap-southeast-1}"; do
  config_args+=(--from-literal="$kv")
done
secret_args=(--from-literal="AMP_SHARED_SECRET=${AMP_SHARED_SECRET:-tuktukpay-amp-demo-secret}")
for key in OPENAI_API_KEY ANTHROPIC_API_KEY AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN; do
  [ -n "${!key:-}" ] && secret_args+=(--from-literal="$key=${!key}")
done
changes=$( {
  kubectl -n "$NAMESPACE" create configmap tuktukpay-config "${config_args[@]}" --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n "$NAMESPACE" create secret generic tuktukpay-secrets "${secret_args[@]}" --dry-run=client -o yaml | kubectl apply -f -
} | tee /dev/stderr | grep -c configured || true)

if [ "$changes" -gt 0 ]; then
  echo "== settings changed: restarting the application pods"
  for d in checkout-api risk-engine payment-router acquirer-sim ledger webhook-dispatcher merchant-sim merchant-copilot \
           shopping-agent wallet-sim merchant-storefront; do
    kubectl -n "$NAMESPACE" rollout restart "deployment/$d" >/dev/null
  done
fi
echo "applied namespace=$NAMESPACE environment=$DEPLOYMENT_ENVIRONMENT images=$IMAGE_REGISTRY/tuktukpay-* version=$SERVICE_VERSION"
echo "watch:   kubectl -n $NAMESPACE get pods -w"
echo "UIs:     k8s/port-forward.sh $NAMESPACE"
