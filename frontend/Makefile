ifeq ($(OS),Windows_NT)
    PYTHON ?= python
else
    PYTHON ?= python3
endif

PNPM = $(PYTHON) ../scripts/pnpm.py

install:
	$(PNPM) install

build:
	$(PNPM) build

dev:
	$(PNPM) dev

test:
	$(PNPM) test

test-e2e:
	$(PNPM) test:e2e

lint:
	$(PNPM) lint

format:
	$(PNPM) format:write

build-static:
	BUILD_MODE=static NEXT_CONFIG_BUILD_OUTPUT=standalone SKIP_ENV_VALIDATION=1 $(PNPM) build
	@if [ -d .next-static/static ]; then mkdir -p .next-static/standalone/.next-static && cp -R .next-static/static .next-static/standalone/.next-static/static; fi
