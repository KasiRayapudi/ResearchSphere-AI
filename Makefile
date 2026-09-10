# ResearchSphere AI - common operations.
# `make help` lists every target.

COMPOSE_DEV  := docker compose
COMPOSE_PROD := docker compose -f docker-compose.prod.yml --env-file .env.prod

.DEFAULT_GOAL := help
.PHONY: help dev dev-down prod prod-build prod-down prod-logs prod-ps \
        validate secret backend-test frontend-build frontend-lint \
        frontend-typecheck verify clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------- development
dev: ## Start the development stack
	$(COMPOSE_DEV) up -d --build

dev-down: ## Stop the development stack
	$(COMPOSE_DEV) down

# ---------------------------------------------------------------- production
prod-build: ## Build production images without starting them
	$(COMPOSE_PROD) build

prod: ## Start the production stack (requires .env.prod)
	@test -f .env.prod || { echo "Missing .env.prod - copy .env.prod.example and fill it in."; exit 1; }
	$(COMPOSE_PROD) up -d --build

prod-down: ## Stop the production stack (volumes are preserved)
	$(COMPOSE_PROD) down

prod-logs: ## Tail production logs
	$(COMPOSE_PROD) logs -f --tail=100

prod-ps: ## Show production service health
	$(COMPOSE_PROD) ps

validate: ## Validate production configuration inside the backend image
	$(COMPOSE_PROD) run --rm --no-deps backend validate

secret: ## Generate a value suitable for SECRET_KEY
	@openssl rand -base64 48

# ---------------------------------------------------------------- local checks
backend-test: ## Run backend verification suites
	cd backend && ./venv/Scripts/python.exe -m pytest -q || true

frontend-build: ## Build the frontend (type-checked)
	cd frontend && npm run build

frontend-lint: ## Lint the frontend
	cd frontend && npm run lint

frontend-typecheck: ## Type-check the frontend
	cd frontend && npm run typecheck

verify: frontend-typecheck frontend-lint frontend-build ## Run every local check

worker-logs: ## Tail the ingestion worker (development stack)
	docker compose logs -f worker

prod-worker-logs: ## Tail the ingestion worker (production stack)
	docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f worker beat

clean: ## Remove build artefacts (never touches volumes or .env files)
	rm -rf frontend/dist backend/__pycache__ backend/**/__pycache__
