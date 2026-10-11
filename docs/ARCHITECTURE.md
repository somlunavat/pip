# Architecture: Parallel Web Agent (Nimble + Agent37 + Gemini)

Oct 11, 2026 · @Som

## Goal and demo story

Build a general-purpose Mac assistant you summon with a hotkey and talk to in your own on-screen overlay, that reads the live web fast through Nimble, hands slow or heavy jobs to parallel Agent37 computers, and notifies you when something you are watching changes.

**One-line pitch:** it reads the web in one API call, works while your laptop sleeps, and learns which changes you care about.

- **Fast reads:** Nimble Search, Extract, and Crawl replace browser automation for anything that only needs to read.
- **Offloaded work:** Agent37 runs long or multi-step jobs on remote Hermes computers, several at once.
- **The novel part:** the agent embeds *changes* (what moved on a page), not pages, and ranks alerts against a preference vector that your replies update.
- **Sponsors exercised:** Nimble (live web) and Gemini (planning, summaries, embeddings).
- **Inspiration, not code:** Moonwalk's Sense, Plan, Act, Verify loop, milestone plans, and parallel groups. Nothing is copied, because most hackathons require work started at the event.

**Demo moment:** one screen, a live timer, the same task run twice: a Hermes worker driving its built-in headless browser versus the Nimble fast path. Then a watched page changes and your overlay shows a ranked alert. Agent37 is infrastructure here, not a sponsor prize.

## Decisions at a glance

We own the orchestrator, call Gemini and Nimble directly, and use Agent37 only for worker computers. Each choice below is a decision to confirm or overturn.

| # | Decision | Choice | Why |
| --- | --- | --- | --- |
| 1 | Where the brain lives | Our own orchestrator that follows Moonwalk's Sense, Plan, Act, Verify loop | Every plan, dispatch, and result passes through code we control and log |
| 2 | Gemini access | Call the Gemini API directly from the orchestrator | Agent37's managed model router is OpenRouter-backed, so usage there may not count as direct Gemini API use for the sponsor prize (confirm with the sponsor) |
| 3 | Fast reads | Nimble called from the orchestrator, in parallel | One round trip, no browser, no remote computer to wake |
| 4 | Heavy work | 2 to 3 always-on `agent37-hermes` workers, one session per task | Sessions on one instance run independently; each worker gets a spend budget |
| 5 | Sleep policy | `auto_sleep` off for demo workers | A rebuild wake takes about two minutes; a demo cannot wait on that |
| 6 | Interface | A Moonwalk-style Electron overlay (pill and card, global hotkey, text and recorded voice) on a local WebSocket to the orchestrator | Streams progress as it happens, looks like a product, and needs no hosting |
| 7 | Watchers | Run in the orchestrator on a timer | Deterministic, cheap, and the data lands straight in SQLite |
| 8 | Storage | SQLite, one local file | Free, no account, and fast enough for a few thousand vectors (cosine similarity in numpy) |
| 9 | Worker output | Text in `output_text`; larger results written to files and pulled by the Files API | The Files API streams any size, unlike exec's 512 KB cap |
| 10 | Tenancy | Single user for the hackathon | Keeps auth, billing, and isolation out of the critical path |

## System overview

The orchestrator is the only component that talks to everything: you and the database sit to its left, and the four things it calls sit to its right.

&#91;embedded content: architecture · 1 orchestrator running a 4-step loop, 4 things it calls\]

A request enters through the Electron overlay, and the loop senses your Mac and has Gemini plan it. Reads go to Nimble in parallel, long jobs go to Agent37 workers, Mac steps run through AppleScript one at a time, and every step is written to SQLite.

## Orchestration loop and the Agent37 dispatch contract

Every request runs Moonwalk's Sense, Plan, Act, Verify loop: it becomes a milestone plan, and each milestone goes to Nimble, a worker session, or an AppleScript step on your Mac. The orchestrator mints session ids itself, so it records a task before the first byte is sent.

**How Moonwalk's loop maps to ours.** Read from the [Moonwalk repo](https://github.com/OactoDev/Moonwalk) on 2026-10-11. We borrow the structure, not the code.

| Moonwalk piece | What it does | Our version |
| --- | --- | --- |
| [perception.py](https://github.com/OactoDev/Moonwalk/blob/main/backend/agent/perception.py) | AppleScript reads the active app, window title, browser URL, and clipboard in parallel (its comments say about 50 ms); a screenshot is taken only when the request implies one | Same, through `osascript` and `screencapture`, feeding the planner |
| Fast path in [core\_v2.py](https://github.com/OactoDev/Moonwalk/blob/main/backend/agent/core_v2.py) | Greetings and simple questions skip planning and get a quick Flash reply | Same, one small Gemini call |
| Milestone types in [planner.py](https://github.com/OactoDev/Moonwalk/blob/main/backend/agent/planner.py) | A plan is milestones, each with a goal, a success signal, hint tools, and `depends_on` | Same JSON, plus a `kind` of `read`, `work`, `local`, or `watch` |
| Plan gate in core\_v2.py | Read-only plans run at once; plans with side-effect tools wait for approval | Same, shown as a plan card with Proceed and Cancel in the overlay |
| [milestone\_executor.py](https://github.com/OactoDev/Moonwalk/blob/main/backend/agent/milestone_executor.py) | A micro-loop where the LLM picks the next tool call until the success signal is met, capped at 50 actions | Stretch goal, for open-ended Mac steps only; Nimble calls are one-shot, and each Hermes worker runs its own loop |
| [sub\_agent\_manager.py](https://github.com/OactoDev/Moonwalk/blob/main/backend/multi_agent/sub_agent_manager.py) | Groups milestones by `depends_on`, runs a group with `asyncio.gather`, and goes sequential if any milestone touches UI tools | Same rule: reads and workers in parallel, Mac steps in order |
| [verifier.py](https://github.com/OactoDev/Moonwalk/blob/main/backend/agent/verifier.py) | Per-tool result checks with confidence scores, plus an optional screenshot check | An evidence check per milestone; no screenshot verification |
| Replanning in sub\_agent\_manager.py | After a failed group, the planner revises the remaining milestones | One retry, then replan the rest |
| Memory in core\_v2.py | Working memory of research snippets, and a background Flash call that stores durable facts | Snippets in SQLite; the fact curator is optional |
| `ws_callback` events in core\_v2.py | `thinking`, `ack`, `doing`, and `response` events drive the overlay | The same event names over a local WebSocket, rendered by the overlay |

1. **Sense.** A request arrives on localhost over a local WebSocket from the Electron overlay, typed or recorded. In parallel, AppleScript reads the active app, window title, browser URL, clipboard, and selected text, and the orchestrator inserts a `tasks` row.
2. **Plan.** Gemini returns a milestone plan as JSON: each milestone has a goal, a success signal, `depends_on`, and a `kind` of `read`, `work`, `local`, or `watch`. Small talk skips the planner with a fast Flash reply.
3. **Gate.** A plan that only reads runs at once. A plan that changes something on your Mac (opens an app, makes a reminder, writes a file) shows a plan card with Proceed and Cancel in the overlay first.
4. **Act.** Group milestones by `depends_on`. Run a group in parallel when it holds only `read` and `work` milestones, and run `local` milestones one at a time, because they share your Mac's UI state. `read` goes to Nimble, `work` goes to a worker, `local` goes to an AppleScript tool, and `watch` goes to the watcher registry.
5. **Dispatch to workers.** `POST https://{workerId}.agent37.app/v1/responses` with header `X-Agent37-Key` and body `{ session_id, input, stream: true, metadata: { task_id, milestone_id } }`. Hermes starts a new thread under any unseen `session_id`, so the id can be minted and stored first.
6. **Track and collect.** The first stream event, `response.created`, returns the response id (needed for cancel). `response.completed` carries `output_text`, `usage`, and `cost_usd`. Larger results are files, pulled with `GET /v1/files/content?path=...`.
7. **Verify.** Check each milestone's result against its success signal: non-empty, source links present for reads, a return value for AppleScript steps. On failure, retry once, then replan the remaining milestones. Gemini then merges the results with source links, the orchestrator speaks and shows the answer, and every step writes a `task_events` row.

**Worker pool.** Create workers once, ahead of the demo, with `POST /v1/instances` using `budget.credit_micros`, `auto_sleep: false`, a `user` tag, and `metadata: { role: "worker" }`. Poll `GET /v1/health` until `healthy` is true. The orchestrator keeps its own in-flight counter per worker, and starts with a cap of 2 concurrent sessions per 2 vCPU worker (an assumption to test, since the reference states no concurrency limit). For a burst, `POST /v1/instances/{id}/fork` copies a worker's disk into a new independent instance, subject to the instance limit.

**Failure handling.** Branch on `status` and on the error `code`, never on HTTP status alone, because a failed turn still returns 200.

| Signal | Meaning | Action |
| --- | --- | --- |
| `409 session_busy` | That session is mid-turn | Use a new session id, or reattach with `GET /v1/responses/{id}/stream` |
| `status: failed` on a 200 | The agent failed the turn | Log the error, retry once on another worker |
| `402` (`quota_exhausted`) | Worker budget or wallet is empty | `POST /v1/instances/{id}/budget/top-up`, or top up the wallet |
| `503 wake_timeout` or `wake_failed` | Instance was asleep and did not wake | Retry; keep demo workers always-on to avoid it |
| `502 container_unreachable` | Briefly down during a restart | Retry with backoff |
| Stream drops mid-turn | Network blip | Reattach with the stored response id; replay holds up to 100,000 events |
| `504 upstream_timeout` | 15 minutes of silence | Read the result from the session transcript |

To see what a worker is doing, `GET /v1/sessions/{id}` returns `active_response_id` (null means the transcript is complete). Sessions list caps at the 100 most recent, so the orchestrator keeps its own task-to-session index.

## Three paths: Nimble, Agent37 workers, and your Mac

Reading goes through Nimble; doing goes to a worker. Gemini tags each subtask at planning time, and the orchestrator enforces a simple rule so the choice is predictable.

|  | Fast path (Nimble) | Worker path (Agent37) |
| --- | --- | --- |
| Use when | Search, extract fields, compare sources, crawl a site | Logged-in sites, forms, multi-step code, file creation, jobs longer than a minute |
| Mechanism | Async HTTP calls from the orchestrator, run in parallel | One Hermes session per subtask on a pooled instance |
| Moving parts | Nimble API only | Instance, health, session, stream, files |
| Failure mode | Rate limit or empty result | Busy session, budget, instance state |
| Result shape | Structured data straight into Gemini | `output_text` or a file |

**Routing rule.** Default to the fast path. Send a subtask to a worker only if it needs a login, submits a form, writes a file, runs code, or Gemini estimates it will exceed about 60 seconds. If a fast-path read comes back empty or blocked, escalate that one subtask to a worker rather than failing the task.

**Fallback routing for reads.** Each `read` milestone tries the cheapest tier first and escalates only when the result is empty, blocked, or fails the evidence check. Every answer records which tier produced it, so the benchmark can report it.

1. **Nimble.** One API call from the orchestrator.
2. **A Hermes worker driving its headless browser.** Slower, but it can click, scroll, and handle pages Nimble returns nothing for.
3. **Stretch: your own Chrome, through a small extension.** For pages that need your logged-in session, which neither Nimble nor a cloud worker has.

Moonwalk's web gateway makes a similar choice between a browser route and a background fetch route, and its verifier treats a thin result as a reason to retry. The router is one function over a list of tiers, so adding the third is additive.

**Local path (your Mac).** A `local` milestone calls one allowlisted AppleScript tool: open an app or URL, create a reminder or note, show a notification, speak with `say`, or read the clipboard or selected text. Local steps run one at a time because they share your Mac's UI state. Moonwalk makes the same choice and falls back to sequential whenever a group touches UI tools. The agent never gets a shell tool.

**Parallel reads.** Fan out with `asyncio.gather` (or `Promise.all`) under a concurrency cap, and record per-call latency. Nimble's rate limits are unknown, so the cap starts low and is tuned after reading its docs.

**Benchmark baseline.** The stock `agent37-hermes` template includes a headless browser, so the baseline is the same read task given to a worker session. Same infrastructure, same prompt, only the access method differs. That makes the comparison fair and gives the demo its timer.

**What this does not claim.** Nimble speeds up reading. It does not log in, click, or submit, and it does not control the local Mac. Those stay on workers.

## Watchers and the change-embedding idea

The agent embeds what *changed* on a page, not the page, and ranks alerts against a preference vector built from your reactions. Most agents embed documents to answer questions; this one embeds events to decide what is worth interrupting you for.

**Watcher loop** (runs in the orchestrator on a timer):

1. Fetch the target through Nimble and normalize it to structured fields (price, status, deadline, listing count).
2. Compare with the last stored snapshot for that watcher.
3. If fields differ, write a short change description, for example "rent 2,400 to 2,200; one new photo; status unchanged".
4. Embed that description with Gemini and store it with the watcher id and timestamp.
5. Score it against the user's preference vector by cosine similarity. At or above the threshold, show it in the overlay, with a macOS notification if the overlay is hidden.

**Preference vector.** Useful and Not useful buttons on each alert card in the overlay update it. A reaction moves a per-user vector toward (or away from) that change's embedding, with older reactions decaying exponentially. No fine-tuning, no labels to collect.

**Retrieval it enables.** Because changes are embedded with timestamps, the agent can answer "show me changes like the price drop on that studio" across sources and weeks, filtered to a time window, using cosine similarity over vectors stored in SQLite.

**Where the novelty is, honestly.** The embedding model is off the shelf. The new parts are what is embedded (deltas, not documents) and how feedback shapes ranking. The demo claim should be "it learned what I care about after four reactions," shown with a before and after ranking, not a claim about a new model.

**Cost and quality guards.** Skip the embedding call when no field changed. Cap watcher frequency. Store the raw diff beside the vector so a human can read why an alert fired.

**Agent37's role.** Watchers that need open-ended reasoning, such as reading a PDF or a login-gated page, can be handed to a worker as a platform cron (the agent can schedule itself with the `agent37 cron` CLI). One such cron is worth including for the demo, since it shows an agent that owns its own schedule.

## Data model in SQLite

Eight tables in one SQLite file cover tasks, the run log, watchers, and vectors. There is no server to run, no account to create, and nothing to fail during the demo.

| Table | Key columns | Purpose |
| --- | --- | --- |
| `tasks` | id, user\_text, status, created\_at, finished\_at | One row per request |
| `subtasks` | id, task\_id, kind, path, worker\_id, session\_id, response\_id | Routing and tracing |
| `task_events` | ts, task\_id, subtask\_id, event, latency\_ms, tokens\_in, tokens\_out, cost\_usd, status, error\_code | Every step, for the latency and cost charts |
| `workers` | instance\_id, status, in\_flight, last\_health\_at | The worker pool |
| `watchers` | id, target, extractor, interval\_s, threshold | What we are watching |
| `snapshots` | ts, watcher\_id, fields (JSON), content\_hash | Each fetch, for diffing |
| `change_events` | id, watcher\_id, ts, description, diff (JSON), embedding (blob), alerted | The embedded changes |
| `preferences` | user\_id, vector (blob), updated\_at, reactions | The preference vector and its history |

**Charts come from plain queries**, not pre-computed views:

- Latency by path per minute: `GROUP BY` path and a minute bucket on `task_events`, with p50 and p95 computed in Python, since SQLite has no percentile function.
- Cost per hour: `SUM(cost_usd)` grouped by hour and by source (Gemini, workers, Nimble).
- Worker load: average `in_flight` per worker from `workers` snapshots.

**Vectors.** Store each embedding as a float32 blob (`numpy.tobytes()`), load a watcher's vectors into one array, and compute cosine similarity in numpy. At a few thousand vectors this is effectively instant. If it ever grows past roughly 100,000, add the `sqlite-vec` extension.

**Conventions.** Turn on WAL mode (`PRAGMA journal_mode=WAL`) so the dashboard can read while the orchestrator writes. Store timestamps in UTC. Keep the vector dimension in one constant, set from the Gemini embedding model chosen at build time. Delete snapshots older than a few days.

**One catch.** The file needs a disk that persists. That works on a laptop behind a tunnel or on a host with a volume (Fly, Railway, a small VPS). It does not work on serverless hosts that wipe the filesystem; there, use a free hosted Postgres instead.

## Logging and the benchmark

Every step writes a row to `task_events`, and a fixed benchmark turns those rows into the demo's headline number. Measure first; do not claim a speedup before it is recorded.

**What each step logs:** a timestamp, `task_id`, `subtask_id`, the step name (plan, route, Nimble call, worker turn, aggregate), latency, token counts, cost, status, and any error code.

**Failures worth logging:** any `status: failed`, any 402, 409, or 503 from Agent37, and any empty Nimble result. These are the real failure modes. A good demo beat is killing a worker mid-task and showing the dashboard record the failure and the router retry on another worker.

**Cost lines.** Agent37 returns `usage` with `cost_usd` on every turn, and `GET /v1/usage` gives workspace spend by day and instance, so the cost chart can be checked against the wallet.

**Benchmark protocol:**

1. Fix five read tasks (for example, "find the three cheapest options for X across four sites").
2. Run each on both paths, three times each, back to back.
3. Record wall-clock time from message received to answer sent.
4. Report the median and the spread per path, and the success rate.
5. Keep the raw rows in the SQLite file so the chart traces back to its numbers.

A wrong or empty answer counts as a failure on its path, so speed is not bought with quality.

**Left out on purpose.** Sentry is not in this build. If you add it later, wrap the same step names as spans.

## Interface layer: Electron overlay and AppleScript

Your own overlay is the agent's face: a small always-on-top pill that grows into a card, talking to the orchestrator over a local WebSocket. Nothing leaves your machine except calls to Gemini, Nimble, and Agent37. This is a cut-down version of Moonwalk's UI, which its README describes as a transparent glass pill with four states and a streaming response card.

**Overlay scope.**

- A frameless, transparent, always-on-top window, summoned by a global hotkey (Electron's `globalShortcut`; Moonwalk uses Cmd+Shift+Space for voice and Option+Space for a type-to-prompt panel).
- The four pill states from Moonwalk: idle, listening, thinking, and doing, plus a response card that renders Markdown with source links.
- Text input, and recorded voice: record with the browser `MediaRecorder`, send the audio to the orchestrator, and pass it to Gemini as audio input to transcribe and answer in one call. Check that Gemini accepts the recording format, and convert with `ffmpeg` if it does not. As far as I know `globalShortcut` fires on key press only, so recording toggles (press to start, press to stop) instead of holding a key.
- A plan card with Proceed and Cancel when a plan will change something, and Useful and Not useful buttons on watcher alerts.
- Live progress lines from `doing` events, and the elapsed time and path used on every answer.

**Wire protocol.** One WebSocket carrying JSON messages that reuse Moonwalk's event names. The client sends `request` (text or audio), `approve`, `cancel`, and `feedback`. The server sends `thinking`, `ack`, `doing` (a milestone label and the path in use), `plan` (for approval), `response` (text, a `pill` or `card` display type, and sources), and `alert`. Bind to 127.0.0.1 and require a token on connect.

**Stack.** The orchestrator is Python (FastAPI with asyncio, which matches Moonwalk's `asyncio.gather` and gives numpy for the vectors). The overlay is Electron with plain HTML, CSS, and JavaScript and no framework.

**Mac tools (AppleScript, allowlisted).** `get_context` (active app, window title, browser URL, selected text, clipboard), `open_app`, `open_url`, `create_reminder`, `create_note`, `show_notification`, and `speak`. Each is one `osascript` call with a short timeout, as in Moonwalk's perception layer. Build `get_context` first and the others only as the demo needs them. No shell tool, no deleting files, no sending messages.

**First-run permissions.** The Electron app needs Microphone access for recording, and the Python process needs Automation access so `osascript` can control System Events and other apps. Grant them in P0 and test before building on them.

**Proactive alerts.** Watchers run inside the orchestrator, so they fire only while your Mac is awake and the orchestrator is running. An alert appears in the overlay, with a macOS notification if the overlay is hidden.

**Not building.** Wake-word detection ("Hey Moonwalk"), cloud mode, and the Chrome extension, which stays a stretch tier in the read router below.

## Security and guardrails

The main risk is untrusted web content steering an agent that can act. The design keeps keys on the server, strips risky tools from workers, and caps spend at every layer.

- **Keys stay in the orchestrator.** The Agent37 `sk_live_` key is effectively root on every instance, because the Files API is not jailed to the workspace. Gemini and Nimble credentials live only in the orchestrator's environment.
- **Worker callbacks use a planted token.** If a worker must call back, set a random token in the instance `env` at create (it is write-only), store its SHA-256 in `metadata`, and verify with a timing-safe compare, the same pattern Agent37's own guides use.
- **Treat web content as data.** Fence Nimble output when passing it to Gemini, never let it into the system prompt, and have the planner ignore instructions found inside fetched pages.
- **Remove tools workers do not need.** Set `AGENT37_MANAGED_PLUGIN_PERFLO_ENABLED=false` and `AGENT37_MANAGED_PLUGIN_COMPOSIO_ENABLED=false` at create. Perflo spends money per call and Composio reaches connected accounts; neither is needed here.
- **No purchases, no outbound messages from workers.** State it in the worker's persona file and do not connect any accounts to worker instances.
- **Spend caps at three layers.** Per-worker `budget` (set explicitly, since the default is zero and managed calls then fail with 402), a global daily cap in the orchestrator, and a kill switch that stops dispatching.
- **Allowlist the human.** Bind the orchestrator to 127.0.0.1 and require a random token when the overlay connects; nothing else can call it.
- **Do not publish worker ports.** A public port carries no credential. Hermes instances get a public webhook port for Telegram by default, which is harmless unless a bot token is connected to the worker; do not connect one.
- **No secrets in images or notes.** Pass credentials through `env` at create; they are write-only afterward.

**Your Mac is the sensitive target.** The agent runs AppleScript as you, so it gets an allowlist of seven tools, no shell, and an approve card in the overlay before any step that changes something. Treat the clipboard and selected text as untrusted, since a hostile web page can put instructions in either.

## Limits, latency, and cost facts

Three always-on workers cost about $0.94 in compute over a 48-hour hackathon, before model spend: $4.76 a month each, metered per minute, so 4.76 x 3 x 48 / 730. Every row below comes from the Agent37 pages linked in its last column, read on 2026-10-11.

| Topic | Fact | Source |
| --- | --- | --- |
| Free tier | One instance on 2 vCPU / 4 GB until the first top-up; the first top-up (from $5) raises the limit to 10 | [Billing](https://www.agent37.com/docs/agents-api/billing) |
| Starter credit | $5 once, after adding a card | [Billing](https://www.agent37.com/docs/agents-api/billing) |
| Compute price | $4.76 a month awake at 2 vCPU / 4 GB; disk only (about $0.36 a month) while asleep | [Billing](https://www.agent37.com/docs/agents-api/billing) |
| Create gate | Needs one day of balance in the wallet; nothing is debited at create | [Billing](https://www.agent37.com/docs/agents-api/billing) |
| Managed model budget | Defaults to $0; managed calls return 402 until a budget is set | [Budgets](https://www.agent37.com/docs/agents-api/budgets) |
| Wake from sleep | Seconds from a checkpoint (5 to 15 s on explicit start); about 2 minutes when rebuilt; first-byte timeout of at least 3 minutes | [Auto-sleep](https://www.agent37.com/docs/agents-api/instance-auto-sleep) |
| Exec | 280 s per command, 512 KB per output stream; runs only on `running` instances and does not wake them | [Run commands](https://www.agent37.com/docs/agents-api/exec) |
| Turn length | No time limit; stream keepalive every 30 s; 15 minutes of silence returns 504 | [Streaming](https://www.agent37.com/docs/agents-api/streaming), [URLs](https://www.agent37.com/docs/agents-api/urls) |
| Concurrency | One turn per session (409 `session_busy`); different sessions on one instance run independently; no per-instance cap is documented | [Sessions](https://www.agent37.com/docs/agents-api/sessions) |
| Stream replay | Buffer of 100,000 events; full replay within about 60 s of finishing, then a synthesized text-only replay for about 30 minutes | [Streaming](https://www.agent37.com/docs/agents-api/streaming) |
| Sessions list | Hermes returns the 100 most recent only | [Sessions](https://www.agent37.com/docs/agents-api/sessions) |
| Crons | 50 per instance, 5-field schedules, missed windows skipped, runs record a `session_id` | [Crons](https://www.agent37.com/docs/agents-api/crons) |
| Fork | Copies the disk (not memory) into an independent instance; counts toward the instance limit | [Fork](https://www.agent37.com/docs/agents-api/instances/fork) |
| Env vars | Set at create, write-only, up to 64 entries | [Create](https://www.agent37.com/docs/agents-api/instances/create) |
| Managed rates | Brave search $0.005 per call; model cost passed through from OpenRouter; Composio $0.000114 per call | [Managed services](https://www.agent37.com/docs/agents-api/managed-services) |
| Files API | Streams any size; keys reach any path on the instance | [Files](https://www.agent37.com/docs/agents-api/files) |

## Build order with a cut line

Five hours, one person: build the fast path, then the overlay, then workers and parallel groups, then the benchmark, then watchers. Each time is a budget, and the cut rules say what to drop when you slip. The overlay is the biggest schedule risk, so if P2 passes 1:30, stop polishing and move on.

1. **P0, setup (0:30).** Top up the Agent37 wallet and create two workers first, so they boot while you code. Then the Python orchestrator with a WebSocket and token, the SQLite schema, and the AppleScript `get_context` tool. Grant the permission prompts now.
2. **P1, fast path (1:00).** Sense, the small-talk fast path, the Gemini milestone plan, parallel Nimble reads with the fallback router, and a synthesized answer with source links. Test it with a tiny terminal WebSocket client.
3. **P2, Electron overlay (1:15).** Frameless pill, global hotkey, text input, `doing` events, and the response card with sources and elapsed time. Add voice recording last. **Cut line A: P1 and P2 together are a demoable project.**
4. **P3, workers and parallel groups (0:45).** Dispatch one `work` milestone to a worker, stream and collect it, group milestones by `depends_on`, run reads and workers in parallel, and add the plan card with Proceed and Cancel for anything that changes your Mac.
5. **P4, benchmark (0:20).** A script that runs the five read tasks on both paths and prints the table. Never cut this; it is the headline.
6. **P5, watchers and change embeddings (0:40).** The user says "watch this", the orchestrator snapshots and diffs it, Gemini embeds the change into SQLite, an alert appears in the overlay, and Useful or Not useful updates the preference vector. **Cut line B: this is the novel part.**
7. **P6, demo prep and buffer (0:30).** Run the benchmark twice, rehearse a two-minute script, and confirm every worker reports `healthy: true`. The spare time is for macOS permission trouble.

**If behind schedule,** drop in this order: voice recording (keep text), the preference vector (keep change detection and plain alerts), the replan step (keep one retry), the second parallel worker, then P5 entirely. Protect P1, P2, and the benchmark.

**Stretch goals, in this order:**

1. The micro-loop from Moonwalk's milestone executor, for open-ended local Mac steps, so the agent can pick the next AppleScript tool call itself until a success signal is met.
2. The Chrome extension as a third read tier, for pages that need your logged-in session.
3. Fork-based burst workers, an agent-scheduled cron, and wake-word detection.

**Removed from this plan:** Siri and Shortcuts, any phone or Telegram link, and a live dashboard (the benchmark script prints a table instead).

## Risks and things not yet verified

The biggest unknown is Nimble, whose API reference has not been read yet. The Agent37 reference was read in part, so a few facts below are assumptions to test.

| Risk or gap | Why it matters | What to do |
| --- | --- | --- |
| Nimble API unread: auth, rate limits, response shapes, MCP server | The fast path and every benchmark depend on it | Read Nimble's docs first; set the concurrency cap from its limits |
| Gemini direct versus via Agent37's router | A router-only setup may not count for the Gemini prize | Call Gemini directly from the orchestrator, as decided; confirm prize rules with the sponsor |
| No documented per-instance concurrency limit | Too many sessions on a 2 vCPU worker may slow every task | Start at 2 per worker, measure, and use fork or a bigger shape if needed |
| Wake latency on stage | A rebuild wake takes about 2 minutes | Keep demo workers always-on and check health before presenting |
| Free tier allows one instance | The pool cannot exist until the wallet is topped up | Top up at the start of P0 (card on hand); worker creation then runs in the background |
| Prompt injection from fetched pages | A hostile page could steer a worker | Fence web text, strip Perflo and Composio, no purchases, spend caps |
| Hermes custom MCP or model config not read | Giving workers Nimble as a tool may need extra setup | Prefer calling Nimble from the orchestrator; if workers need it, ship a small CLI that reads a key from `env` |
| macOS permissions and the overlay | Automation and Microphone prompts, plus Electron window quirks (always-on-top, focus, transparency), can eat an hour | Do the permission prompts in P0, test each tool, and keep a plain terminal client as a fallback |
| Event rules | Most hackathons require work started at the event; Moonwalk is only inspiration | Write everything fresh and say so in the submission |
| Event date and judging format unknown | Affects scope and the demo script | Confirm the schedule and submission deadline |

**Reference coverage.** About 33 of the roughly 75 Agent37 pages were read: concepts, billing, budgets, chat, streaming, sessions, files, models, crons, exec, auto-sleep, URLs, public ports, errors, instance create and fork, lifecycle, templates, managed services, messaging, iMessage, and the Instinct, Dots, and Grok Bot guides, among others. Not read: instance get, list, edit, resize, backups, and object pages; sizing; logs; metrics; SSH; custom images; the Grok, OpenCode, and Pi harness pages; desktop; n8n; domains; white-label; the integrations page; and several example guides. Nothing in this design depends on those, but read them before relying on any of their behavior.

## Answers and what is still open

Seven questions are settled and two remain.

**Settled**

- [x] Team and hours: one person, five hours. The build order is cut to fit.
- [x] Category: none. The agent answers any question and does any task, and a watcher is whatever you ask it to watch.
- [x] Interface: your own Electron overlay on your Mac, modeled on Moonwalk's pill and card, not Siri, Telegram, iMessage, or any phone link.
- [x] Agent37 wallet: you will top it up, and a card is on hand.
- [x] Voice and text: both, through the overlay, with typed input and a toggled recording sent to Gemini as audio.
- [x] Gemini direct: yes, call the Gemini API directly. Still worth a quick check with the sponsor at their booth.
- [x] Where it runs: your Mac, on localhost, since the interface is local. No tunnel or hosting.

**Still open**

- [ ] Submission deadline and demo slot length.
- [ ] Your macOS version, since microphone and Automation permission prompts differ slightly by version.
