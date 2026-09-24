# Cautious Crypto Bro



Telegram crypto-signal interpretation and deterministic execution on Bybit

Demo.



```text

Telegram text / images / albums

&#x20;       ↓

multimodal LLM signal extraction

&#x20;       ↓

OPEN / REDUCE / CLOSE normalization

&#x20;       ↓

Strategy V2 deterministic planning

&#x20;       ↓

manual approval or configured AUTO routing

&#x20;       ↓

Bybit Demo

&#x20;       ↓

durable PositionSupervisor reconciliation
```


The LLM interprets trader posts. It does not control position size, order

geometry, risk limits, lifecycle quantities, or exchange mutation directly.



This project supports Bybit Demo only. Do not use production API keys.



Strategy V2



New OPEN signals use Strategy V2.

Executable OPEN signals require a symbol, LONG/SHORT direction, and an explicit
LIMIT price, RANGE, or MARKET instruction. Stop loss and take profit are optional;
the extractor never invents prices. Missing SL uses
`StrategyV2Policy.fallback_stop_distance_pct` (default 2%): below the live MARKET
price or LIMIT price for LONG, above for SHORT. RANGE uses the adverse edge
(LONG: lower boundary; SHORT: upper boundary). Stops round outward to exchange
ticks. Trader stops take precedence. Execution plans store the concrete stop and
`stop_loss_source` (`TRADER` or `POLICY`), displayed on approval cards.
Risk budget sizes positions after choosing the stop; it does not set stop distance.
Actual MARKET fills retain that original stop. Missing entry semantics, symbol,
or direction remain non-executable. Policy-derived targets retain existing R rules.



Default entry risk:



E1 60%

E2 25%

E3 15%



For MARKET entries, E1 executes first. Its actual Bybit fill is confirmed and

persisted before E2/E3 are calculated at 0.33R and 0.66R toward the original

stop.



Adverse E1 slippage can reduce E2/E3 size so total worst-case stop loss remains

inside the frozen risk budget.



Default exits:



TP1     25% at +0.50R

TP2     25% at +1.00R

TP3     25% at +1.50R

Runner  25%



Trailing protection activates at +0.50R with a 0.30R trailing distance.



See STRATEGY.md for the complete execution contract.



Safety model

Bybit Demo only.

LLM output never directly mutates the exchange.

Position sizing and order geometry are deterministic.

Missing execution-critical values are never invented.

Live totalWalletBalance is frozen into each new execution plan.

All-entry worst-case loss must remain within the configured risk budget.

MARKET E1 fill is confirmed before E2/E3 are submitted.

Initial E1 catastrophe protection is verified before the supervisor replaces

per-entry protection with the position-level stop.

REDUCE/CLOSE require explicit evidence in the current Telegram text/caption.

Images may provide symbol/side context but cannot independently authorize a

destructive action.

Lifecycle execution uses reduce-only market orders and confirms the resulting

live Bybit position.

Submitted-but-unconfirmed lifecycle actions become UNCERTAIN.

Strategy reconciliation uses Bybit as the live source of truth.

Profit protection never moves backward automatically.

The system does not use real-money Bybit API endpoints.

Approval modes



Manual approval is the default:



AUTO\_APPROVAL\_MODE=disabled



Supported modes are:



disabled

open\_only

all



open\_only allows only OPEN signals that pass the AUTO safety checks to execute

without pressing the Telegram button.



all also allows eligible REDUCE/CLOSE actions to use AUTO routing.



AUTO execution is still constrained by the same deterministic planner,

lifecycle evidence rules, account-state checks, execution lock, Bybit Demo

executor, and recovery quarantine.



Quick start

Requirements

Docker + Docker Compose

Python 3.12 for local development

uv

a Telegram account

an LLM provider account

a Bybit account with Demo Trading enabled



Clone the repository and create the local environment file:



cp .env.example .env

Credentials



Fill .env with:



Telegram API ID/hash;

Telegram bot token;

Telegram approver user/chat IDs;

source Telegram channel IDs;

OpenRouter and/or OpenCode Go credentials;

Bybit Demo API key/secret.



Example source configuration:



TELEGRAM\_SOURCE\_CHANNELS=\[-1001234567890,-1009876543210]

AUTO\_APPROVAL\_MODE=disabled

Telegram authorization



Build the app:



docker compose build app



Run the Telegram dialog helper:



docker compose run --rm -it \\

&#x20; --entrypoint /app/.venv/bin/python \\

&#x20; app scripts/list\_telegram\_dialogs.py

Start the app

docker compose up -d app

docker compose logs -f app



The service performs the configured startup lookback and then listens for live

Telegram posts.



Manual OPEN approval cards show:



signal entry and stop;

E1/E2/E3;

fixed exits and runner;

frozen live wallet capital;

exact deterministic maximum planned loss;

current account/exposure context.



REDUCE/CLOSE cards display current account context and are revalidated against

live Bybit state at execution time.



Runtime architecture



Important production modules:



execution.py

&#x20;   deterministic Strategy V2 planner



execution\_coordinator.py

&#x20;   serialized exchange mutations and lifecycle confirmation



bybit.py

&#x20;   Bybit Demo HTTP execution primitives



position\_supervisor.py

&#x20;   durable live strategy reconciliation



storage.py

&#x20;   SQLite intents, plans, strategy state, migrations



service.py

&#x20;   Telegram signal orchestration and approval/AUTO routing



PositionSupervisor and execution callbacks share one account mutation lock so

the supervisor cannot reconcile through the middle of another exchange

mutation.



Development



Install dependencies and Git hooks:



uv sync --python 3.12 --group dev

uv run pre-commit install



Run static checks and tests locally:



uv run ruff format --check .

uv run ruff check .

uv run pyright

uv run pytest -q

uv lock --check

uv run pre-commit run --all-files

git diff --check



Runtime/integration checks that require Redis, Telegram, Bybit Demo, or LLM

credentials should run through Docker Compose.



See TESTING.md for replay, diagnostics, and runtime testing.



Current release status: TODO.md.
