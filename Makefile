# ResearchSphere AI - common operations.
# `make help` lists every target.

COMPOSE_DEV   := docker compose

# The production stack runs published images. docker-compose.prod.yml carries
# no build: stanza, so a deployment host needs no application source; the
# build overlay adds those stanzas back for producing the images from this
# working tree. Everything that does not build uses the base file alone.
PROD_FILES    := -f docker-compose.prod.yml
BUILD_FILES   := -f docker-compose.prod.yml -f docker-compose.prod.build.yml
COMPOSE_PROD  := docker compose $(PROD_FILES) --env-file .env.prod
COMPOSE_BUILD := docker compose $(BUILD_FILES) --env-file .env.prod

.DEFAULT_GOAL := help
.PHONY: help dev dev-down prod prod-build prod-pull prod-down prod-logs prod-ps \
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
prod-build: ## Build the production images from this tree without starting them
	$(COMPOSE_BUILD) build

prod: ## Build from this tree and start the production stack
	@test -f .env.prod || { echo "Missing .env.prod - copy .env.prod.example and fill it in."; exit 1; }
	$(COMPOSE_BUILD) up -d --build

prod-pull: ## Start the production stack from published GHCR images (no build)
	@test -f .env.prod || { echo "Missing .env.prod - copy .env.prod.example and fill it in."; exit 1; }
	@tag="$$(sed -n 's/^IMAGE_TAG=//p' .env.prod | tail -n 1)"; case "$$tag" in "") echo "IMAGE_TAG is not set in .env.prod."; exit 1;; local) echo "IMAGE_TAG=local names an image built by 'make prod', not a published one."; echo "Set it to a released version, e.g. 1.2.0 for Git tag v1.2.0."; exit 1;; *) echo "Pulling images tagged $$tag";; esac
	$(COMPOSE_PROD) pull
	$(COMPOSE_PROD) up -d --no-build

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

migrate: ## Apply database migrations (development stack)
	docker compose run --rm backend migrate

migrate-status: ## Show the current and head revisions (development stack)
	docker compose run --rm backend python -m alembic current -v

migration: ## Create a migration from model changes: make migration m="add x"
	cd backend && ./venv/Scripts/python -m alembic revision --autogenerate -m "$(m)"

prod-migrate: ## Apply database migrations (production stack)
	$(COMPOSE_PROD) run --rm backend migrate

prod-stamp: ## Mark an existing pre-Alembic database as current WITHOUT migrating
	$(COMPOSE_PROD) run --rm backend stamp

worker-logs: ## Tail the ingestion worker (development stack)
	docker compose logs -f worker

prod-worker-logs: ## Tail the ingestion worker (production stack)
	$(COMPOSE_PROD) logs -f worker beat

clean: ## Remove build artefacts (never touches volumes or .env files)
	rm -rf frontend/dist backend/__pycache__ backend/**/__pycache__
