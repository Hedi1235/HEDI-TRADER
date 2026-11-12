# XAU/USD Paper-Trading Supervisor

A Python research and simulation system that analyzes gold-market inputs, applies deterministic risk limits, and records paper trades without connecting to a live broker. Yahoo Finance's `GC=F` gold-futures quote is used as a clearly labeled proxy because its `XAUUSD=X` symbol returned no history during verification.

## Python paper-trading system

- `uv run python -m gold_trader.main --once` — run one XAU/USD analysis cycle
- `uv run python -m gold_trader.main` — repeat paper-trading cycles every 15 minutes
- `uv sync --extra mt5` then `uv run --extra mt5 python -m gold_trader.main_mt5` — local Windows MT5 demo companion; never run this on Replit/Linux
- `uv run python -m unittest discover -s tests -v` — run Python unit tests
- Add `FRED_API_KEY` through Replit Secrets to enable macro data; without it, the system records HOLD
- The Replit runner is paper-only. The separate Windows runner hard-rejects any MT5 account that is not confirmed DEMO.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `gold_trader/` — Python market-data, analysis, deterministic risk, paper execution, and Windows MT5 demo bridge
- `gold_trader/README.md` — local setup, paper and demo runner modes, and risk behavior
- `.env.example` — local configuration template; never commit `.env`
- `tests/` — Python unit tests for schemas, risk limits, paper ledger, and fail-closed behavior
- Existing `artifacts/`, `lib/`, and `scripts/` remain the TypeScript workspace

## Architecture decisions

- The Replit workflow remains paper-only; MT5 order submission exists only in a separate Windows runner that enforces demo mode.
- Missing, stale, or invalid market/macro data must produce HOLD, never synthetic input.
- Geopolitical analysis only uses headlines explicitly supplied in the local JSON input.

## Product

The project includes a Python XAU/USD research and paper-trading supervisor with technical and macro inputs, typed agent reports, deterministic risk checks, and a SQLite simulation ledger.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

_Populate as you build — sharp edges, "always run X before Y" rules._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
