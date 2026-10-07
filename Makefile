# Convenience targets for the TukTukPay workshop stack
COMPOSE ?= docker compose
CHAOS   ?= http://localhost:8090

.PHONY: up down restart logs ps smoke pause resume reset act1 act2 act3 act4 act5 act6 act7 baseline dotnet dualship flags hec-test logs-status k8s-up k8s-forward k8s-down

# ---- Kubernetes (the reference deployment) ----
k8s-up:        ## create/update the kind cluster, build + load images, install the Splunk collector chart, deploy
	k8s/kind-up.sh
k8s-forward:   ## port-forward the UIs and APIs to localhost (self-healing)
	k8s/port-forward.sh
k8s-down:      ## delete the kind cluster
	k8s/port-forward.sh stop; kind delete cluster --name $${CLUSTER:-tuktukpay}

# ---- Docker Compose (alternative) ----
up:            ## build + start everything
	$(COMPOSE) up -d --build
down:          ## stop and remove containers + database volume
	$(COMPOSE) down -v
restart:
	$(COMPOSE) restart
logs:
	$(COMPOSE) logs -f --tail=100
ps:
	$(COMPOSE) ps
smoke:         ## send test payments + copilot questions
	scripts/smoke-test.sh
pause:         ## stop simulated traffic (payments + copilot questions) to save laptop CPU
	curl -s -X POST $(CHAOS)/traffic -H 'Content-Type: application/json' -d '{"paused": true}'; echo
resume:        ## restart simulated traffic
	curl -s -X POST $(CHAOS)/traffic -H 'Content-Type: application/json' -d '{"paused": false}'; echo
flags:
	curl -s $(CHAOS)/flags | python3 -m json.tool
reset baseline act1 act2 act3 act4 act5 act6 act7:
	curl -s -X POST $(CHAOS)/scenarios/$(if $(filter reset,$@),baseline,$@) | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("title","reset"))'
hec-test:      ## send one test event to the Splunk Cloud HEC endpoint configured in .env (docs/logs-setup.md)
	@set -a; . ./.env; set +a; \
	curl -sS --max-time 15 -w ' [http %{http_code}]\n' "$$SPLUNK_HEC_URL/event" \
	  -H "Authorization: Splunk $$SPLUNK_HEC_TOKEN" -H 'Content-Type: application/json' \
	  -d "{\"event\":\"tuktukpay hec connectivity test\",\"sourcetype\":\"otel\",\"source\":\"hec-test\",\"index\":\"$$SPLUNK_HEC_INDEX\",\"fields\":{\"service.name\":\"hec-test\",\"deployment.environment\":\"$$DEPLOYMENT_ENVIRONMENT\"}}"
logs-status:   ## how many log records the collector has sent to / failed to send to Splunk HEC
	@docker run --rm --network container:otel-collector curlimages/curl:8.10.1 -s http://127.0.0.1:8888/metrics \
	  | grep -E '^otelcol_exporter_(sent|send_failed|enqueue_failed)_log_records' | grep -v profiling || echo "collector metrics not reachable"
dotnet:        ## swap the ledger for the .NET implementation
	$(COMPOSE) -f docker-compose.yml -f docker-compose.dotnet.yml up -d --build ledger
dualship:      ## collector exports to Splunk AND Datadog (needs DD_API_KEY in .env)
	$(COMPOSE) -f docker-compose.yml -f docker-compose.dualship.yml up -d otel-collector
