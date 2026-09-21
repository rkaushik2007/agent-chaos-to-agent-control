# Every target is a one-line wrapper over `uv run demo ...`.
#
# `make` is not installed everywhere (notably not on a stock Windows box), so
# the canonical commands are the `uv run demo ...` forms. They behave
# identically on macOS, Linux and Windows and need nothing but uv.
#
#   make act1   ==   uv run demo act1
#
# See docs/STAGE_RUNBOOK.md for the exact sequence used on stage.

UV ?= uv

.DEFAULT_GOAL := help
.PHONY: help install act1 act2 act3 act4 rehearse reset seed console doctor up down test live-toolbox live-tools

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Create the venv and install everything (mock + live + hooks + dev)
	$(UV) sync --all-extras --group dev

act1:  ## Act 1 - Chaos
	$(UV) run demo act1

act2:  ## Act 2 - Identity + Toolbox
	$(UV) run demo act2

act3:  ## Act 3 - Enforcement
	$(UV) run demo act3

act4:  ## Act 4 - Visibility
	$(UV) run demo act4

rehearse:  ## Run all four acts in MOCK, non-interactive, asserting every decision
	$(UV) run demo rehearse

reset:  ## Wipe the audit DB, reseed data, drop the chaos dotfile
	$(UV) run demo reset

seed:  ## Regenerate data/*.json
	$(UV) run demo seed

console:  ## Serve the governance console on http://localhost:8000
	$(UV) run demo console

doctor:  ## Pre-talk environment check
	$(UV) run demo doctor

up:  ## Start the trace UI (Aspire Dashboard on http://localhost:18888)
	docker compose up -d

down:  ## Stop the trace UI
	docker compose down

test:  ## Run the test suite
	$(UV) run pytest -q

live-toolbox:  ## LIVE only - create/update the Foundry toolbox from config/toolbox.yaml
	$(UV) run python infra/create_toolbox.py

live-tools:  ## LIVE only - run the tool servers and the tunnel Foundry calls (leave running)
	$(UV) run python infra/start_live_tools.py
