SHELL := /bin/bash
COMPOSE_FILE := deploy/docker-compose.yml

.PHONY: up down doctor seed run test chaos bench lint

up:
	docker compose -f $(COMPOSE_FILE) up -d --wait

down:
	docker compose -f $(COMPOSE_FILE) down -v

doctor:
	python3 scripts/preflight.py

seed:
	python3 scripts/seed_branches.py

lint:
	python3 -m py_compile sdwan_bridge/steering.py sdwan_bridge/server.py scripts/preflight.py scripts/seed_branches.py ledger/hash_log.py tests/test_sdwan_bridge.py

run:
	AWS_ENDPOINT_URL=http://localhost:4566 \
	OPA_URL=http://localhost:8181 \
	SDWAN_AUDIT_BUCKET=sdwan-multicloud-backbone-audit \
	WEBHOOK_HMAC_SECRET=local-dev-sdwan-backbone-secret \
	uvicorn sdwan_bridge.server:app --host 0.0.0.0 --port 8080

test:
	python3 -m unittest discover -s tests -p "test_*.py" -v

chaos:
	python3 -m unittest tests/test_sdwan_bridge.py -k test_chaos_stale_token_rejected_after_failover -v

bench:
	python3 -m unittest tests/test_sdwan_bridge.py -k test_bench_lease_and_log -v
