#!/usr/bin/env bash
# Roll a deployment back to its previous revision (the fix for the bad-image / oversize acts).
#   k8s/acts/rollback.sh <deployment> [namespace]
set -euo pipefail
DEP=${1:?deployment name}
NS=${2:-tuktukpay}
kubectl -n "$NS" rollout undo "deployment/$DEP"
kubectl -n "$NS" rollout status "deployment/$DEP" --timeout=180s
