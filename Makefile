.DEFAULT_GOAL := help
SHELL := bash

help: ## show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

guard: ## fail if internal/confidential content leaked into tracked files
	bash scripts/check-no-internal.sh

fmt: ## format rust + python
	cargo fmt --manifest-path shinkend/Cargo.toml
	cd sdk/python && ruff format .

lint: ## lint rust + python + typescript (matches CI)
	cargo fmt --manifest-path shinkend/Cargo.toml -- --check
	cargo clippy --manifest-path shinkend/Cargo.toml --all-targets -- -D warnings
	cd sdk/python && ruff check .
	npm run ts:check

test: ## test rust + python + typescript
	cargo test --manifest-path shinkend/Cargo.toml --all
	cd sdk/python && pytest -q
	npm run ts:test

run: ## run the Guest Runtime (ws://127.0.0.1:8765)
	cargo run --manifest-path shinkend/Cargo.toml

sandbox-image: ## build the Linux Sandbox docker image
	docker build -f images/linux/Dockerfile -t shinken/sandbox-linux .

sandbox-bench: ## run a one-sandbox local Docker provider benchmark
	PYTHONPATH=sdk/python/src python scripts/sandbox_bench.py --provider docker --concurrency 1 --iterations 1

benchmarks: ## run every local benchmark suite (Docker; regenerates results JSON + plots)
	bash benchmarks/run_all.sh

.PHONY: help guard fmt lint test run sandbox-image sandbox-bench benchmarks
