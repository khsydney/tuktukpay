# Convenience targets for the TukTukPay workshop stack
COMPOSE ?= docker compose
CHAOS   ?= http://localhost:8090

.PHONY: up down restart logs ps smoke pause resume reset act1 act2 act3 act4 act5 baseline dotnet dualship flags

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
reset baseline act1 act2 act3 act4 act5:
	curl -s -X POST $(CHAOS)/scenarios/$(if $(filter reset,$@),baseline,$@) | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("title","reset"))'
dotnet:        ## swap the ledger for the .NET implementation
	$(COMPOSE) -f docker-compose.yml -f docker-compose.dotnet.yml up -d --build ledger
dualship:      ## collector exports to Splunk AND Datadog (needs DD_API_KEY in .env)
	$(COMPOSE) -f docker-compose.yml -f docker-compose.dualship.yml up -d otel-collector
