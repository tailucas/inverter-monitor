.DELETE_ON_ERROR:
.DEFAULT_GOAL := help

USER_ID := 999
GROUP_ID := 999

DOCKER_URL := https://docs.docker.com/engine/install
DOCKER_COMPOSE_URL := https://docs.docker.com/compose/install
DEVCLI_URL := https://code.visualstudio.com/docs/devcontainers/devcontainer-cli
CHECK_USER := vscode

.PHONY: help check dev dev-build dev-up datadir python configure build push run rund

# ---------- Dev container (host only) ----------

check:
	@if [ "${USER}" = "$(CHECK_USER)" ]; then \
	  echo "Running as user ${USER}; dev container targets are host-only."; \
	  echo "Inside the dev container, use: make build|run|rund|python"; \
	  exit 1; \
	fi
	@which docker > /dev/null || (echo "Needs Docker with compose, see $(DOCKER_URL) and $(DOCKER_COMPOSE_URL)"; exit 1)
	@which devcontainer > /dev/null || (echo "Needs Dev Container CLI; see $(DEVCLI_URL)"; exit 1)

dev-build: check ## Build the dev container
	devcontainer build --workspace-folder .

dev-up: dev-build ## Start the dev container
	devcontainer up --workspace-folder .

dev: dev-up ## Open a shell in the dev container
	devcontainer exec --workspace-folder . bash

# ---------- Application (host or dev container) ----------

data/: ## Create ./data/ owned by USER_ID:GROUP_ID (runs only if missing)
	mkdir -p ./data/
	sudo chown $(USER_ID):$(GROUP_ID) ./data/
	sudo chmod 755 ./data/
	sudo chmod g+rws ./data/
	sudo rm -f ./data/app-std* ./data/cron-std* ./data/supervisor.sock

datadir: data/ ## Set up ./data/ (alias)

.venv: pyproject.toml uv.lock ## Create/sync the Python virtual environment
	@uv -V
	uv python install
	uv sync
	@touch .venv

python: .venv ## Set up the Python virtual environment (alias)

.env: base.env docker-compose.yml ## Generate .env from base.env and the cred store
	@docker -v
	@docker compose version
	rm -f $@
	cp base.env $@
	echo "" >> $@
	docker compose run --remove-orphans app /opt/app/dot_env_setup.sh >> $@
	test $$(wc -l < $@) -ge 2

configure: build .env ## Generate runtime configuration (.env)

build: ## Build the app container image
	@docker -v
	docker compose --env-file base.env --progress plain build

push: build ## Push the built image to Docker Hub
	@docker compose images
	docker compose push

run: data/ build .env ## Run the app container (foreground)
	@test -f docker-compose.yml
	@docker ps | grep 1password || (echo "1Password container not running."; exit 1)
	@test -d ./data/
	docker compose up --remove-orphans

rund: data/ build .env ## Run the app container (detached)
	@test -f docker-compose.yml
	@docker ps | grep 1password || (echo "1Password container not running."; exit 1)
	@test -d ./data/
	docker compose up -d --remove-orphans

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_\/.-]+:.*## / {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
