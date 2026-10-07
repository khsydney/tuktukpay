#!/usr/bin/env bash
# Kubernetes-only act: a rollout whose resource request no node can satisfy. The new
# risk-engine pod stays Pending (Unschedulable), the deployment never reaches its
# desired replicas -> AutoDetect "K8s pod phase pending/failed" + "deployment is not at
# spec" and the Terraform detectors fire; the AI Troubleshooting Agent explains it and
# the Remediation Plan proposes the fix. The old pod keeps serving (rolling update).
#   k8s/acts/oversize-rollout.sh [namespace]     # undo: k8s/acts/rollback.sh risk-engine
set -euo pipefail
NS=${1:-tuktukpay}
kubectl -n "$NS" patch deployment risk-engine --type=json -p '[
  {"op":"replace","path":"/spec/template/spec/containers/0/resources/requests/memory","value":"64Gi"}
]'
echo "risk-engine now requests 64Gi memory: watch 'kubectl -n $NS get pods' for a Pending pod (Insufficient memory)."
echo "undo: k8s/acts/rollback.sh risk-engine $NS"
