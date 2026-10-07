#!/usr/bin/env bash
# Port-forward the TukTukPay UIs and APIs from the cluster to localhost, in the background.
#   k8s/port-forward.sh [namespace]      # default: tuktukpay
#   k8s/port-forward.sh stop             # stop them
set -euo pipefail
NS=${1:-tuktukpay}
PIDFILE=${TMPDIR:-/tmp}/tuktukpay-port-forward.pids
if [ "$NS" = "stop" ]; then
  [ -f "$PIDFILE" ] && xargs kill < "$PIDFILE" 2>/dev/null; rm -f "$PIDFILE"; echo "port-forwards stopped"; exit 0
fi
: > "$PIDFILE"
for spec in chaos-controller:8090 checkout-api:8080 merchant-copilot:8086 shopping-agent:8087 wallet-sim:8088 merchant-sim:8085; do
  svc=${spec%%:*}; port=${spec##*:}
  kubectl -n "$NS" port-forward "svc/$svc" "$port:$port" >/dev/null 2>&1 &
  echo $! >> "$PIDFILE"
done
sleep 2
echo "forwarded: http://localhost:8090 (war room)  http://localhost:8087 (agent console)  :8080 (payments)  :8086 (copilot)  :8088 (wallet)  :8085 (merchant-sim)"
echo "stop with: k8s/port-forward.sh stop"
