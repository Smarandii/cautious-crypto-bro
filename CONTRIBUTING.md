# Contributing

Thanks for helping. This project submits real orders to an exchange, so the bar
is "provably correct and reviewable" rather than "clever". Read
[STRATEGY.md](STRATEGY.md) before changing anything in `execution`,
`execution_coordinator.py`, `position_supervisor.py` or `bybit/`.

By contributing you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
Contributions are licensed GPL-3.0-only, matching [LICENSE](LICENSE).

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --frozen --group dev
uv run pre-commit install
```

## The gate

`master` requires CI to pass, so every change lands through a pull request even
if you are the only contributor. Four workflows run:

- `ci.yml` &mdash; format, lint, types, build, tests, Dockerfile and Compose
- `codeql.yml` &mdash; CodeQL security-extended analysis of Python
- `dependency-review.yml` &mdash; advisory gate on the PR's dependency delta
- `pages.yml` &mdash; deploys the landing page, only when `docs/` changes

Locally, the same checks as `ci.yml`:

```sh
uv sync --frozen --group dev
uv run ruff format --check src scripts tests
uv run ruff check src scripts tests
uv run pyright
uv run pytest -q
uv lock --check
uv build
uv run pre-commit run --all-files
git diff --check
```

`pre-commit run --all-files` is a superset, so running it alone is acceptable
locally. `uv run pyright` type-checks `src/` only. `uv run pytest -q` runs with
offline/mocked boundaries and real temporary SQLite — no network, no
credentials, no orders. If it needs anything else, the test is wrong.

## Dependency and workflow updates

`.github/dependabot.yml` opens weekly PRs for `uv` and `github-actions`, grouped
into runtime and dev-tooling batches. Do not merge one blind: a dependency bump
can change extraction or execution behaviour, and the project is GPL-3.0-only.

Actions are pinned to commit SHAs. Dependabot rewrites both the SHA and the
trailing version comment. pre-commit hook revs live in `.pre-commit-config.yaml`
and have no Dependabot ecosystem &mdash; update those with `pre-commit autoupdate`.

## Code conventions

Enforced by `ruff` (line length 88, `py312`) and `pyright` (basic mode):

- `[tool.ruff.lint.mccabe] max-complexity = 10` applies to `src/`. `scripts/`
  and `tests/` are exempt, because they are linear run-once procedures.
- If a change trips `C901`, the answer is to decompose the function into named
  steps, not to raise the ceiling. Recent history is almost entirely that.
- Extract shared behaviour into a named module rather than growing a god
  object. `bybit/normalize.py`, `approval_presenter.py` and
  `position_management/` exist because of this.
- Pass request/response DTOs instead of widening keyword-argument lists.
- Pydantic models own the domain invariants and their `Field` constraints;
  parsing and validation stay out of the transport and storage layers.
- Keep planning **deterministic**. No wall-clock reads, no randomness, no
  network calls inside planning. Freezing capital and deriving price-risk
  budgets is what makes replay comparisons meaningful.

## Tests

- One file per behavior area, named `tests/test_<area>.py`, mirroring the module
  it covers. `tests/fakes.py` holds the shared fakes; extend it instead of
  redefining doubles locally.
- Add a regression test for every bug fix, reproducing the failure before the
  fix. `tests/test_audit_regressions.py` is the precedent.
- Every new branch in `src/` needs a test that fails without it.
- Deterministic time and ordering. No sleeps to synchronize.
- Anything touching exchange responses needs a malformed/partial-payload case.
- `uv run pytest -q` measures branch coverage and fails below the `fail_under`
  floor in `pyproject.toml`. New code without tests drops the number and fails
  the gate. Raise the floor when you improve coverage; never lower it.

## Commit and PR conventions

Conventional Commits, matching existing history:

```
refactor(execution): extract scale-in leg derivation from rebase
fix(telegram): recover skipped signal posts
ci: gate application complexity at C901=10
```

Use a scope matching the touched module. Imperative mood, no trailing period,
one logical change per commit.

Before opening a PR, fill in `.github/pull_request_template.md` and confirm the
gate output you pasted is from your own run. Say plainly whether the change
alters planning determinism, `UNCERTAIN` handling, source claiming, or approval
gating — reviewers will focus there.

## Safety rules for contributors

- **Demo only.** Never add code, docs, or defaults that point at mainnet. Real
  trading is out of scope for this repository.
- **Never loosen a fail-closed path.** Pre-submit failures closing empty
  strategies, quarantining potentially-accepted submissions, and parking
  `UNCERTAIN` strategies are deliberate. Make them stricter if you must touch
  them; never retry ambiguous operations automatically.
- **No secrets.** No `.env`, no Telegram session files, no API keys, no audit
  bundles with real payloads, in commits, issues, or PRs. `.gitignore` already
  covers these, but a regenerated `repomix-output.xml` will happily inline live
  `.env` contents — check before attaching it.
- **Live scripts stay opt-in.** `scripts/smoke_bybit_trade.py --execute`,
  `scripts/run_bybit_demo_e2e.py --execute-demo`, and the audit scripts call
  paid providers or place Demo orders. Default to preview; gate execution behind
  an explicit flag and pin limits. See [TESTING.md](TESTING.md).
- Report anything that looks like a security defect via
  [SECURITY.md](SECURITY.md), not a public issue.

## What is welcome

- Test coverage for untested branches, especially around partial fills, stop
  handoff, quarantine and restart recovery.
- Multilingual extraction coverage for mixed entry/lifecycle posts.
- Reductions in real complexity, in the decompose-don't-raise-ceiling sense.
- Documentation corrections where the code disagrees with [STRATEGY.md],
  [TESTING.md], or [TODO.md](TODO.md).

Open an issue first for large behavioral changes to execution, planning, or
approval, so the design is agreed before the code is written. Strategy changes
usually start in [TODO.md](TODO.md).

## Contributor checklist

- [ ] `uv run pre-commit run --all-files` passes
- [ ] `uv run pytest -q` passes with no new skips
- [ ] new branch has a test that fails without the change
- [ ] no secrets, `.env`, session files, or audit payloads added
- [ ] PR describes determinism / `UNCERTAIN` / claiming / gating impact
- [ ] behavior changes reflected in [STRATEGY.md](STRATEGY.md) or [TODO.md](TODO.md)
- [ ] no mainnet path introduced
