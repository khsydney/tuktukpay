#!/usr/bin/env bash
# Port-forward the TukTukPay UIs and APIs from the cluster to localhost, in the background.
# Not needed on kind: kind-config.yaml publishes the NodePorts on localhost directly. Use this on
# clusters without such mappings. Each forward runs in a restart loop: `kubectl port-forward`
# quits whenever the pod behind the service restarts (Acts 6/7 do that on purpose) or a
# connection errors, which would otherwise leave the war-room panel dead in the browser.
#   k8s/port-forward.sh [namespace]      # default: tuktukpay
#   k8s/port-forward.sh stop             # stop them
set -euo pipefail
NS=${1:-tuktukpay}
PIDFILE=${TMPDIR:-/tmp}/tuktukpay-port-forward.pids
if [ "$NS" = "stop" ]; then
  if [ -f "$PIDFILE" ]; then
    while read -r pid; do pkill -P "$pid" 2>/dev/null || true; kill "$pid" 2>/dev/null || true; done < "$PIDFILE"
    rm -f "$PIDFILE"
  fi
  pkill -f "kubectl -n [a-z0-9-]* port-forward svc/" 2>/dev/null || true
  echo "port-forwards stopped"; exit 0
fi
"$0" stop >/dev/null 2>&1 || true
: > "$PIDFILE"
for spec in chaos-controller:8090 checkout-api:8080 merchant-copilot:8086 shopping-agent:8087 wallet-sim:8088 merchant-sim:8085; do
  svc=${spec%%:*}; port=${spec##*:}
  ( while true; do kubectl -n "$NS" port-forward "svc/$svc" "$port:$port" >/dev/null 2>&1 || true; sleep 1; done ) &
  echo $! >> "$PIDFILE"
done
sleep 3
echo "forwarded: http://localhost:8090 (war room)  http://localhost:8087 (agent console)  :8080 (payments)  :8086 (copilot)  :8088 (wallet)  :8085 (merchant-sim)"
echo "stop with: k8s/port-forward.sh stop"
