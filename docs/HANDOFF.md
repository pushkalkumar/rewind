# Handoff — state of Rewind as of 2026-09-12 (morning of Frontier Cascadia)

Everything needed to run and demo is in this repository. Nothing pending lives outside git except caches.

## Setting up on a new machine

```
git clone https://github.com/pushkalkumar/rewind && cd rewind
cd backend && pip install -r requirements.txt && cd ..
cd frontend && npm install && npm run build && cd ..      # dist/ is committed, so this is optional until you change the UI
cd backend && python cli.py serve                          # http://127.0.0.1:8000/?demo=1
```

Requirements: Python 3.10+ (3.11 fine), Node 20, git. For **live** runs with no API keys, install Claude Code and log in (`claude` on PATH); the `claude-cli:sonnet` / `claude-cli:haiku` drivers shell out to it. With keys, copy `.env.example` to `.env`. Docker is auto-detected if installed (never exercised yet — see below). `gh auth login` again on the new machine if you want to push from it.

Optional carry-over from the old machine: `.rewind/runs.sqlite` (run history; enables `/print?run=<id>` and `python cli.py runs`). The demo fixture `frontend/src/demo/run.json` is already a real run and needs nothing.

## What is built and proven

- **Miner + verifier** (`backend/rewind/gitmine.py`, `verify.py`): subject-line fix heuristic, parent + test files brought forward, fail-to-pass by construction, regression baseline = tests passing at both broken and fixed states (SWE-bench P2P rule), renamed/deleted test files handled, unicode-safe on Windows.
- **Sandboxes** (`backend/rewind/sandbox/`): `local` proven; `docker` and `daytona` written against the same ABC with command-recording tests, **never executed** (no Docker or key on the dev machine). First thing to do on a machine with Docker: `REWIND_SANDBOX=docker python cli.py bench marshmallow-code/marshmallow --max 1 --models claude-cli:haiku`.
- **Agent loop** (`agent.py`): four tools; `write_file` has whole-file and `old`/`new` replace modes; `read_file` takes `start`/`end`; 15-step cap. Agent sees the commit **subject** + failing test ids only.
- **LLM drivers** (`llm/`): `claude-cli` proven (flat JSON schema via `--json-schema`, `--input-format stream-json`, `CLAUDECODE` env stripped); `anthropic` and `openai`/`lilac` drivers written with fake-client tests, not run against a real endpoint yet. Lilac default base URL `https://api.getlilac.com/v1`.
- **Scoring** (`scoring.py`): FIXED / CHEATED (test, conftest, CI, runner-config paths; skip/xfail markers) / BROKE; grade A ≥ .9, B ≥ .7, C ≥ .5, D ≥ .3.
- **Server** (`server.py`, `runner.py`, `db.py`): SSE with `?after=`, per-repo lock, db-polling streams for runs owned by another process, orphan recovery with a 15-minute grace.
- **Frontend**: one page, report card, receipt drawer with red cheat lines, `/print`, `?demo=1` offline replay (phase-budgeted to ~22 s), `?run=<id>` deep links.
- **Tests**: `cd backend && python -m pytest tests -q -k "not live"` → 105 passed. Live CLI tests: drop the `-k`.

## The real run behind the demo

`marshmallow-code/marshmallow`, 400 commits → 16 candidates → 5 verified. Sonnet A (5/5, 6.6 steps, 62 s/task); Haiku 4.5 B (4/5, 13.6 steps, 209 s/task, cap hit 3×). No cheats, no regressions. Run id `c3cfeec1`.

## Highest-value next steps

1. **Get a cheat receipt honestly.** With a Lilac key at the event: `REWIND_MODELS=claude-cli:sonnet,lilac:<open model>` in `.env`, then `python cli.py bench marshmallow-code/marshmallow` (~25 min), `python cli.py export <id>`, `cd frontend && npm run build`. An open model is far likelier to edit a test.
2. Exercise the Docker backend once on a machine that has Docker.
3. Optional harder repo for variety: `more-itertools/more-itertools` verifies 5 instances but each takes ~70 s (its one test file is the whole suite).

## Decisions worth knowing before changing things

- Subject-only issue text: commit bodies routinely narrate the fix.
- Whole-file `write_file` alone made 70 KB files a harness problem, not a model problem — hence replace mode.
- Grades tightened so 4/5 reads as B; the effort line (steps · seconds) is the honest differentiator when both models pass.
- Demo fixture must stay a real export — no synthetic data anywhere outside `frontend/src/demo/run.json`, and that file is real too.
