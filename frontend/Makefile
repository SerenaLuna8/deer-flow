install:
	pnpm install

build:
	pnpm build

dev:
	pnpm dev

test:
	pnpm test

test-e2e:
	pnpm test:e2e

lint:
	pnpm lint

format:
	pnpm format:write

build-static:
	BUILD_MODE=static NEXT_CONFIG_BUILD_OUTPUT=standalone SKIP_ENV_VALIDATION=1 pnpm build
	@if [ -d .next-static/static ]; then mkdir -p .next-static/standalone/.next-static && cp -R .next-static/static .next-static/standalone/.next-static/static; fi
