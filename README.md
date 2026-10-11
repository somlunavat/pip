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
| `local` | One allowlisted AppleScript tool; writing into your apps (reminders, notes, calendar events) needs **Proceed** | one at a time |
| `watch` | Registers a watcher that diffs structured fields and embeds the change | in parallel |

**Results, and tabs when you want them.** Pip answers with a one-line headline and picks
the pages you'd act on (the best results, or a prefilled search such as Google Flights).
It opens them as tabs only when you ask ("open…", "pull up…", "show me in my browser");
otherwise they're one click away under **Open sources**. `PIP_OPEN_RESULTS=always` or
`never` overrides that. Only URLs from the answer's own sources or well-known search
sites are ever opened.

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
                                           # close: Esc, the × button, or double-click outside

# Fallback client, no Electron needed
python scripts/ws_client.py "compare prices for a Sony WH-1000XM5"

# Hand Pip a task without the overlay; it keeps running, and when it finishes or needs
# your OK a Pip popup appears top-right. Click it to open the overlay on that task.
python -m orchestrator.ask "find a cheap flight to Austin next Friday"
python -m orchestrator.ask --wait "what's on my calendar today"   # print the answer here
```

### Permissions

Pip can do more the more macOS lets it. Grant everything once, approving each macOS
prompt yourself:

```bash
python -m orchestrator.permissions          # walks through every prompt
python -m orchestrator.permissions --check  # shows what's granted
```

Or type `setup` in the overlay for the same list with a **Grant** button per item.

| Permission | What it unlocks |
| --- | --- |
| Accessibility | reading selected text, typing into apps |
| Screen Recording | "what's on my screen?" |
| Automation: System Events, Calendar, Reminders, Notes, Mail, Music, Finder, Shortcuts, Chrome/Safari | reading and acting in each app |
| Microphone (asked by the overlay) | voice requests |

macOS attaches these to the app that launches the orchestrator (Terminal, iTerm, or
VS Code). If you start Pip from a different app later, grant them again there.

### Agent37 workers

```bash
# Needs a topped-up wallet: the free tier allows one instance at a time.
python scripts/create_workers.py --count 2 --credit 3 --cap 10

# Free tier, or you already made an instance in the dashboard: use it instead.
python scripts/create_workers.py --list
python scripts/create_workers.py --adopt <instance-id>

# Paste the printed AGENT37_WORKERS=... line into .env and restart the orchestrator.
```

Workers are created always-on (`auto_sleep: false`), with an explicit budget, and with
the Perflo and Composio managed plugins turned off.

With workers configured, Pip splits big jobs ("compare these 6 companies and make me a
spreadsheet") into pieces that run on workers at the same time, then merges them. Any
files a worker makes are downloaded to `~/Downloads/Pip/<task>/` and shown in Finder.

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
| `PIP_OPEN_RESULTS` | `1` | Open results as browser tabs after each answer |

## Security model

- Keys live only in the orchestrator's environment. The Agent37 `sk_live_` key is
  effectively root on every instance, so it is never sent to a worker.
- The server binds to `127.0.0.1` and requires a random token on connect.
- Web pages, worker output, the clipboard, and selected text are all treated as
  untrusted: they are fenced as data before reaching Gemini and never placed in a
  system prompt.
- On the Mac the agent gets a small allowlist of AppleScript tools and no shell.
  Arguments are passed through argv, never spliced into script source. Tools that
  write data need approval; tools that only show something (open a page or app, a
  notification) don't.
- Plans are validated before anything runs: work that would need the Mac is never
  sent to a remote worker, and requests nothing can do get a straight "can't do that".
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
