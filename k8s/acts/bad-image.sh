#!/usr/bin/env bash
# Kubernetes-only act: a rollout with an image tag that does not exist. The new
# merchant-copilot pod goes ImagePullBackOff, the deployment stays below its desired
# replicas -> Kubernetes alerts; the AI Troubleshooting Agent names the tag and the
# Remediation Plan proposes the rollback. The old pod keeps serving (rolling update).
#   k8s/acts/bad-image.sh [namespace]     # undo: k8s/acts/rollback.sh merchant-copilot
set -euo pipefail
NS=${1:-tuktukpay}
IMG=$(kubectl -n "$NS" get deployment merchant-copilot -o jsonpath='{.spec.template.spec.containers[0].image}')
kubectl -n "$NS" set image deployment/merchant-copilot "merchant-copilot=${IMG%%:*}:2.1.0-rc3"
echo "merchant-copilot rolling out image ${IMG%%:*}:2.1.0-rc3 (does not exist): expect ImagePullBackOff."
echo "undo: k8s/acts/rollback.sh merchant-copilot $NS"
