# Same targets and names as github.com/srghosh56/gitops-microservice-demo.
CLUSTER ?= gitops-demo
IMAGE   ?= llm-gateway
TAG     ?= 0.1.0
NS      ?= llm-gateway

.PHONY: test lint loadtest build up down kind-load helm-check argocd-app pf-app bootstrap-k8s

test:            ## Run the test suite (needs the test Postgres, see README)
	uv run pytest

lint:            ## Lint, format check and type check
	uv run ruff check . && uv run ruff format --check . && uv run mypy

loadtest:        ## 30 s Locust run against a running gateway (GATEWAY_URL, GATEWAY_ADMIN_API_KEY)
	LOADTEST_API_KEY=$$(uv run python loadtest/setup_tenant.py) \
	  uv run locust -f loadtest/locustfile.py --headless -u 20 -r 10 -t 30s \
	  --host $${GATEWAY_URL:-http://localhost:8000} --only-summary

build:           ## Build the container image
	docker build -t $(IMAGE):$(TAG) .

up:              ## Start the local docker compose stack
	docker compose up -d --build

down:            ## Stop the local docker compose stack
	docker compose down

kind-load: build ## Copy the image into the kind node
	kind load docker-image $(IMAGE):$(TAG) --name $(CLUSTER)

helm-check:      ## Lint and render the chart
	helm lint charts/llm-gateway
	helm template llm-gateway charts/llm-gateway --namespace $(NS)

argocd-app:      ## Register the app with Argo CD (after `make kind-load`)
	kubectl apply -f argocd/application.yaml

pf-app:          ## Gateway on http://localhost:8091
	kubectl -n $(NS) port-forward svc/llm-gateway 8091:80

bootstrap-k8s:   ## Deploy the model + demo tenant through the admin API (run pf-app first)
	GATEWAY_URL=http://localhost:8091 GATEWAY_ADMIN_API_KEY=change-me-admin-key ./scripts/bootstrap.sh
