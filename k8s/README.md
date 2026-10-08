# TukTukPay on Kubernetes — the recommended facilitator deployment

This variant runs the same platform on a Kubernetes cluster with the **Splunk OpenTelemetry Collector Helm chart**
and the **OpenTelemetry Operator**, so the Java, Node.js (and optionally .NET) services are instrumented by a pod
annotation instead of anything in the image or the code.

**Why Kubernetes for the facilitator stack:** the Splunk AI Troubleshooting Agent runs on APM service alerts, APM business
transaction errors and Kubernetes infrastructure alerts, but **Remediation Plans are Kubernetes-only** and the agent's root-cause
analysis draws on Kubernetes entities, pod logs and related resources
([docs](https://help.splunk.com/en/splunk-observability-cloud/create-alerts-detectors-and-service-level-objectives/create-alerts-and-detectors/ai-troubleshooting-agent-and-remediation-plan)).
Three acts exist only to show that: **Act 6** (ledger memory leak → OOMKilled → restarts), **Act 7** (wallet-sim crash-loops after a
bad release → CrashLoopBackOff → agents fail closed) and the script-driven rollouts below (unschedulable request, bad image tag). Act 2
also gains a Kubernetes angle: `risk-engine` has a 500m CPU limit, so model v3 saturates it and the "container CPU at its limit"
detector fires. Docker Compose stays the quick option for laptop-based teams — it has no Kubernetes alerts, so no remediation plans.

## 0. Fastest path: a local `kind` cluster (no EKS needed)

```bash
# requirements: docker, kind, kubectl, helm; SPLUNK_REALM + SPLUNK_ACCESS_TOKEN (and SPLUNK_HEC_* for logs) in .env
k8s/kind-up.sh                                                        # cluster "tuktukpay", namespace tuktukpay, env tuktukpay-k8s
NAMESPACE=tuktukpay-team01 DEPLOYMENT_ENVIRONMENT=tuktukpay-team01 k8s/kind-up.sh   # add a team namespace on the same cluster
open http://localhost:8090                                            # war room; :8087 agent console, :8080 payments API, :8086 copilot ...
scripts/smoke-test.sh                                                 # (ports are published by kind — no port-forward needed)
```
The script builds the images with Compose, loads them into kind, installs the Helm chart with the operator (and the Splunk Platform
log exporter when `SPLUNK_HEC_URL`/`SPLUNK_HEC_TOKEN` are set), applies the manifests and prints the next steps. A `t3.xlarge`
handles one cluster with two team namespaces at spike ×10; a laptop with 8 GB for Docker runs one namespace at ×3.

## 1. Build and push images

```bash
export IMAGE_REGISTRY=123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/tuktukpay   # or ghcr.io/<org>
docker compose build
for s in chaos-controller checkout-api risk-engine payment-router acquirer-sim ledger webhook-dispatcher merchant-copilot wallet-sim loadgen; do
  docker tag tuktukpay-workshop-$s:latest $IMAGE_REGISTRY/tuktukpay-$s:latest   # compose names images <project>-<service>
  docker push $IMAGE_REGISTRY/tuktukpay-$s:latest
done
# optional .NET ledger
docker build -t $IMAGE_REGISTRY/tuktukpay-ledger-dotnet:latest services/ledger-dotnet && docker push $IMAGE_REGISTRY/tuktukpay-ledger-dotnet:latest
```

## 2. Install the collector + operator

```bash
helm repo add splunk-otel-collector-chart https://signalfx.github.io/splunk-otel-collector-chart && helm repo update
helm upgrade --install splunk-otel-collector splunk-otel-collector-chart/splunk-otel-collector \
  --namespace splunk-otel --create-namespace \
  --set splunkObservability.accessToken=$SPLUNK_ACCESS_TOKEN \
  --set splunkObservability.realm=$SPLUNK_REALM \
  -f k8s/values-splunk-otel-collector.yaml
kubectl -n splunk-otel get instrumentation        # -> splunk-otel-collector
```

## 3. Deploy TukTukPay

```bash
IMAGE_REGISTRY=$IMAGE_REGISTRY NAMESPACE=tuktukpay DEPLOYMENT_ENVIRONMENT=tuktukpay-eks k8s/apply.sh
kubectl -n tuktukpay get pods -w
kubectl -n tuktukpay port-forward svc/chaos-controller 8090:8090   # facilitator panel
kubectl -n tuktukpay port-forward svc/checkout-api 8080:8080       # scripts/smoke-test.sh works against this
```

What to show:

* `kubectl -n tuktukpay describe pod -l app=payment-router` → the operator added an init container and
  `JAVA_TOOL_OPTIONS=-javaagent:/otel-auto-instrumentation-java/javaagent.jar`. The image and the code did not change.
* `acquirer-sim` / `ledger` run plain `node server.js`; the operator injected `NODE_OPTIONS`.
* **Kubernetes Navigator** in Splunk shows the `tuktukpay` namespace; **Related Content** from any span lands on the pod, and the
  application logs carry `k8s.pod.name`, `k8s.namespace.name`, `k8s.cluster.name` (added by the chart's `k8sattributes` processor).
* The **AI Troubleshooting Agent** produces a **Remediation Plan** for Kubernetes alerts. On APM alerts (checkout-api p90) the agent
  still runs root-cause analysis; only the plan is Kubernetes-only.

## The Kubernetes acts

| Act | Start | What Kubernetes sees | Alert (Terraform; Splunk AutoDetect has an org-wide twin) | Evidence logs | Fix |
|---|---|---|---|---|---|
| **6 — The leaky ledger** | panel `act6` (flag `ledger_memory_leak`) | `ledger` RSS climbs 6 MiB/s to its 384Mi limit → **OOMKilled** → restart, repeat | *Kubernetes container restarted*; *container memory above 85% of its limit* ("K8s container restart count is > 0") | ledger `event=memory.pressure` ("settlement cache holds 300 MiB and is not evicting", `memory.leaked_mb`, `container.memory.limit_mb`), checkout-api `event=ledger.write_failed` | flag off (the plan will say: raise the limit / roll back the release) |
| **7 — The bad rollout** | panel `act7` (flags `wallet_crash_loop` + `agent_traffic_surge`) | `wallet-sim` exits 3 a few seconds after start → **CrashLoopBackOff**; AMP agent payments fail closed | *Kubernetes container restarted*; *deployment below desired replicas* | wallet-sim `event=service.crashed` ("schema migration kya_agents_v2 did not apply; exiting with code 3", `deployment.release`), checkout-api `mandate.block_reason=mandate_kya_unavailable` | flag off (≈ `kubectl rollout undo`) |
| Oversize rollout | `k8s/acts/oversize-rollout.sh` | new `risk-engine` pod **Pending** (Insufficient memory), deployment stays below spec; old pod keeps serving | *Kubernetes pod pending or failed*; *deployment below desired replicas* | Kubernetes events (`FailedScheduling`) | `k8s/acts/rollback.sh risk-engine` |
| Bad image | `k8s/acts/bad-image.sh` | new `merchant-copilot` pod **ImagePullBackOff** | *deployment below desired replicas* | Kubernetes events (`ErrImagePull`) | `k8s/acts/rollback.sh merchant-copilot` |
| 2 on Kubernetes | panel `act2` | `risk-engine` pins its 500m CPU limit under model v3 | *container CPU at its limit (throttling)* + the APM `risk-engine p90` alert | `event=config.changed`, `event=risk.inference_slow` | flag off; the plan proposes the limit change — the real fix is the model |

Run `terraform apply -var realm=<realm> -var environment=<env> -var k8s_namespace=<ns>` once so the detectors exist; then start an act,
wait for the alert, open it → **AI Troubleshooting Agent** → read the root cause and the Evidence tab → **Remediation Plan**. The
per-act playbook (expected answers, prompts, debrief) is in [`docs/ai-troubleshooting.md`](../docs/ai-troubleshooting.md).

## 4. .NET variant

Edit the `ledger` Deployment: image `tuktukpay-ledger-dotnet`, `command: ["dotnet", "Ledger.dll"]`, annotation
`instrumentation.opentelemetry.io/inject-dotnet: "splunk-otel/splunk-otel-collector"`. Same API, same traces, .NET runtime
metrics and AlwaysOn Profiling for .NET.

## Logs

The applications export their own OTLP log records (the same JSON events as on Docker Compose, with `trace_id`,
`span_id` and `service.name`), so the chart only has to forward them: fill in the `splunkPlatform` block of
`k8s/values-splunk-otel-collector.yaml` (HEC endpoint, token, index) and keep `logsCollection.containers.enabled: false`
to avoid every line arriving twice. The Node.js services need `SPLUNK_AUTOMATIC_LOG_COLLECTION=true` and an
`OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` on the agent's 4318 port (already in `tuktukpay.yaml`). Setup of the Splunk
Cloud Platform side and of Log Observer Connect: [`docs/logs-setup.md`](../docs/logs-setup.md). Pod logs carry
`k8s.*` fields, which is what lets the AI Troubleshooting Agent's Remediation Plan reason about the workload.

## Reaching the UIs

`kind-config.yaml` maps the node ports 30090/30087/30080/30086/30088/30085 to the same-numbered `localhost` ports
(8090 war room, 8087 agent console, 8080 payments API, 8086 copilot, 8088 wallet, 8085 merchant-sim); the six Services
are `type: NodePort` with those fixed ports. No `kubectl port-forward` is involved, so pod restarts during Acts 6/7
cannot break the panel. On clusters created without the mappings (EKS …) use `k8s/port-forward.sh`, which restarts
its tunnels automatically, or expose the Services the way the cluster prefers (Ingress, LoadBalancer).

## Configuration comes from `.env`

`k8s/apply.sh` (called by `kind-up.sh`) turns the application settings in `.env` into a ConfigMap `tuktukpay-config`
(`BASE_RPS`, `COPILOT_*`, `AGENT_*`, `LOG_LEVEL`, `AWS_REGION`) and a Secret `tuktukpay-secrets` (`AMP_SHARED_SECRET`,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, AWS credentials); every application container reads both through `envFrom`, and
when either object changed the application pods are rolled (Postgres and Redis are not). So the workflow after editing
`.env` is the same as on Compose: run `k8s/kind-up.sh` (or `make k8s-up`). Explicit `env` entries in the manifest take
precedence over `envFrom`, which is why the per-service values were removed from `tuktukpay.yaml`.

The same command picks up **code changes**: the images are rebuilt and loaded, and the deployments whose image actually changed
on the node are restarted (the manifests use a fixed `latest` tag with `IfNotPresent`, so a rebuilt image would otherwise keep the
old container running — that bit us on 7 Oct). A restart of `chaos-controller` resets the flags and the traffic switch; start the
act again afterwards.

## Notes

* In `tuktukpay.yaml` the `SPLUNK_OTEL_AGENT` (node IP) variable must stay **first** in every container's `env` list: Kubernetes only
  expands `$(VAR)` references to variables defined earlier in the list, so an `OTEL_EXPORTER_OTLP_ENDPOINT` placed before it is the
  literal string `http://$(SPLUNK_OTEL_AGENT):4317` and nothing is exported (the symptom is `Failed to export ... $(SPLUNK_OTEL_AGENT):4317`
  in the Python pods' logs and no spans in APM).
* Chart 0.161 renamed the agent components the values file references (`k8s_attributes`, `resource_detection`, `kubelet_stats`)
  and dropped `splunkObservability.logsEnabled`; the values match that version. If `helm upgrade` complains about a schema key or a
  renamed component after a chart update, `helm template` the chart with default values and mirror its pipeline names.
* On kind/minikube the kubelet's self-signed certificate makes the `kubelet_stats` scrape fail silently (no container CPU/memory
  metrics, so the memory/CPU detectors and the pod charts stay empty); the values set `insecure_skip_verify: true` for it. Remove
  that on EKS/GKE/AKS.
* A laptop that sleeps (lid closed, idle timer) freezes the Docker Desktop VM and with it the whole cluster: every signal stops
  in the same minute and resumes on wake — followed by a minute of CoreDNS `no such host` errors and, on a VPN, usually a new
  egress address (next bullet). `make awake` (`caffeinate -dimsu`) stops idle sleep; nothing stops clamshell sleep, so keep the
  lid open during the session. `pmset -g log | grep -E "Sleep|Wake"` tells you afterwards what happened.
* `helm upgrade` (inside `kind-up.sh`) ending in `context deadline exceeded`, the collector agent pod stuck `Terminating`, and —
  after a `kubectl delete pod --force` — the replacement agent in CrashLoopBackOff with `listen tcp 127.0.0.1:8889: bind: address
  already in use`: the forced delete removed the pod object but the old container kept running on the node (seen after a
  sleep/wake cycle). While that lasts **nothing** is exported — traces and metrics included. Fix: on the node,
  `docker exec tuktukpay-control-plane crictl ps -a | grep otel-collector`, then `crictl stop <id>; crictl rm <id>` for the container
  whose pod no longer exists (and `crictl stopp/rmp` its sandbox); the DaemonSet recovers within a minute. Re-run `k8s/kind-up.sh`
  afterwards so the Helm release is recorded as deployed again.
* A laptop on a VPN: when the egress IP changes, the Splunk Cloud HEC allow list blocks the collector (`splunk_hec/platform_logs`
  timeouts, `sending queue is full`; `make hec-test` → `http 000`) until the new address is added, and kind's CoreDNS (which forwards
  to the host resolver) fails lookups for a few minutes (`lookup ingest.<realm>… no such host`), so traces and metrics show a gap
  that heals on its own. Check with `kubectl -n splunk-otel logs ds/splunk-otel-collector-agent --since=10m | grep -c "Exporting failed"`
  and `make k8s-logs-status` (spans, metric points and log records sent / failed per exporter, read from the agent's own metrics
  through a throw-away `hostNetwork` pod — the agent image has no shell and binds its metrics port to localhost).
* Postgres and Redis are dev-grade Deployments here; use RDS/ElastiCache for anything longer than a workshop.
* `deployment.environment` comes from the Helm value `environment`; the manifests also set it in `OTEL_RESOURCE_ATTRIBUTES`
  so the environment is consistent whichever path the telemetry takes.
* Python services keep `opentelemetry-instrument` in their command (the Splunk distro); Go uses the SDK in code.
