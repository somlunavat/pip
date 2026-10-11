# pip

A Mac assistant you summon with a hotkey. It reads the live web in one API call through
[Nimble](https://docs.nimbleway.com), hands slow or heavy jobs to parallel
[Agent37](https://www.agent37.com/docs) Hermes computers, plans with
[Gemini](https://ai.google.dev), and tells you when a page you're watching changes in a
way you care about.

> It reads the web in one API call, works while your laptop sleeps, and learns which
> changes you care about.

The design is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). The structure is
inspired by [Moonwalk](https://github.com/OactoDev/Moonwalk), but none of its code is
used here.

## How it works

```
 Electron overlay ──ws://127.0.0.1──▶  Orchestrator (FastAPI, asyncio)
   pill + cards                         Sense → Plan → Gate → Act → Verify
                                          │        │               │
                                          │     Gemini      ┌──────┼──────────┬────────────┐
                                          │  (plan, synth,  ▼      ▼          ▼            ▼
                                          │   embed, audio) Nimble  Agent37   AppleScript  Watchers
                                          ▼                 (read)  workers   (7 tools)    (timer)
                                        SQLite (WAL): tasks, task_events, watchers, change vectors
```

| Milestone kind | Path | Runs |
| --- | --- | --- |
| `read` | Nimble search/extract → escalates to a worker's headless browser if the result is empty, thin, or blocked | in parallel |
| `work` | Agent37 Hermes worker session (login, forms, files, code, or over ~60 s) | in parallel |
| `local` | One allowlisted AppleScript tool; side effects need **Proceed** in the overlay | one at a time |
| `watch` | Registers a watcher that diffs structured fields and embeds the change | in parallel |

**Change embeddings.** Watchers embed *what changed* ("rent 2,400 to 2,200 (down, 8%)"),
not the page. Each alert is scored against a preference vector that your Useful / Not
useful reactions shape, with older reactions decaying over time.

## Quick start

Requirements: macOS, Python 3.11+, Node 20+, and API keys for Gemini and Nimble.
Agent37 workers are optional; without them `work` milestones fail cleanly and reads
stay on Nimble.

```bash
# 1. Orchestrator
uv venv && uv pip install -e ".[dev]"      # or: python -m venv .venv && pip install -e ".[dev]"
cp .env.example .env                       # fill in GEMINI_API_KEY and NIMBLE_API_KEY
python -m orchestrator                     # ws://127.0.0.1:8779/ws, token in data/overlay_token

# 2. Overlay (new terminal)
cd overlay && npm install && npm start     # Option+Space to type, Cmd+Shift+Space to talk

# Fallback client, no Electron needed
python scripts/ws_client.py "compare prices for a Sony WH-1000XM5"
```

On the first run macOS asks for **Microphone** access (the overlay) and **Automation**
access (for `osascript` to control System Events, Reminders, and Notes). Grant both.

### Agent37 workers

```bash
# Needs a topped-up wallet: the free tier allows one instance.
python scripts/create_workers.py --count 2 --credit 3 --cap 10
# Paste the printed AGENT37_WORKERS=... line into .env and restart the orchestrator.
```

Workers are created always-on (`auto_sleep: false`), with an explicit budget, and with
the Perflo and Composio managed plugins turned off.

### Benchmark

```bash
python scripts/benchmark.py                 # 5 read tasks × 3 repeats × {nimble, worker}
python scripts/benchmark.py --only nimble   # no workers needed
```

Any wrong or empty answer counts as a failure on its path. Raw rows are stored in
`task_events` (`event = 'bench'`).

## Configuration

All settings come from the environment (see [`.env.example`](.env.example)).

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEMINI_PLAN_MODEL` | `gemini-3.5-flash` | Planning and synthesis |
| `GEMINI_FAST_MODEL` | `gemini-3.5-flash-lite` | Small talk, field extraction, transcription |
| `GEMINI_EMBED_MODEL` / `EMBED_DIM` | `gemini-embedding-2` / `768` | Change embeddings |
| `NIMBLE_CONCURRENCY` | `4` | Parallel Nimble calls |
| `AGENT37_WORKERS` | — | Comma-separated worker instance ids |
| `AGENT37_SESSIONS_PER_WORKER` | `2` | In-flight cap per worker (to be measured) |
| `PIP_DAILY_CAP_USD` | `10` | Orchestrator-wide daily spend cap |
| `PIP_KILL_SWITCH` | `0` | `1` stops all paid dispatching |

## Security model

- Keys live only in the orchestrator's environment. The Agent37 `sk_live_` key is
  effectively root on every instance, so it is never sent to a worker.
- The server binds to `127.0.0.1` and requires a random token on connect.
- Web pages, worker output, the clipboard, and selected text are all treated as
  untrusted: they are fenced as data before reaching Gemini and never placed in a
  system prompt.
- On the Mac the agent gets seven allowlisted AppleScript tools and no shell.
  Arguments are passed through argv, never spliced into script source.
- Spending is capped at three layers: a per-worker Agent37 budget, a global daily
  cap, and a kill switch.

## Development

```bash
.venv/bin/pytest -q          # unit tests (no network; fakes for Gemini, Nimble, Agent37)
.venv/bin/ruff check .       # lint
cd overlay && npm run check  # overlay syntax check
```

```
orchestrator/
  loop.py         Sense, Plan, Gate, Act, Verify
  router.py       routing rule + tiered read fallback
  verifier.py     per-milestone evidence checks
  nimble.py       Nimble search / extract
  agent37.py      worker SSE client, pool, dispatcher, control plane
  llm/gemini.py   triage, plan, synthesize, extract, transcribe, embed
  mac.py          allowlisted AppleScript tools
  watchers.py     snapshots, diffs, change embeddings, alerts
  preferences.py  decaying preference vector
  server.py       token-gated WebSocket server
  db.py           SQLite schema and helpers
overlay/          Electron pill + cards
scripts/          create_workers, benchmark, ws_client
```

Work is tracked in GitHub issues. Each change goes on a branch
(`feat/…`, `fix/…`, `docs/…`, `ci/…`) and lands through a pull request that links its
issue. Commits follow [Conventional Commits](https://www.conventionalcommits.org).
