# GoldPulse — UI Reference

GoldPulse is an XAUUSD scalping bot for MT5, optionally powered by Claude Code for agentic trade decisions (see the Agent Mode section below). This document explains how `ui.py` was built, why each decision was made, and how to extend it. It is intended as context for future Claude sessions working on this codebase.

---

## File overview

| File | Purpose |
|------|---------|
| `main.py` | The bot — single-file async MT5 trading engine. Never modified by the UI. |
| `ui.py` | The dashboard — launches `main.py` as a subprocess, streams its output, persists history. |
| `agent.py` | Agentic mode — an LLM-driven layer that decides when to open/close trades. Runs in its own background thread inside the UI process. |
| `trade_history.json` | Auto-created. Persistent log of all closed trades (JSON array). |
| `ui_settings.json` | Auto-created. Last-used values for every UI field, restored on next launch. |
| `agent_settings.json` | Auto-created. Agent Mode panel settings (prompt, schedule, guardrails), separate from `ui_settings.json`. |
| `logs/agent/agent_YYYYMMDD.jsonl` / `.log` | Auto-created. The agent's own decision log — separate from `main.py`'s execution logs under `logs/`. |

---

## How to run

```bash
uv run python ui.py
```

`main.py` is never called directly when using the UI. The UI builds and executes the full CLI command internally.

---

## Architecture decisions

### Why subprocess, not import?

`main.py` is designed to be a standalone CLI program. It calls `asyncio.run()`, `sys.exit()`, and `input()` directly — importing it and calling its functions would require restructuring the entire bot. Subprocess keeps the two files completely independent and means the bot can still be run from the command line unchanged.

### Why tkinter?

- Zero extra dependencies — tkinter is Python stdlib on Windows with Python 3.14.
- No build step, no npm, no Electron.
- Adequate performance for a trading dashboard that updates at 200 ms intervals.
- Runs in the same process as the UI event loop with no bridging complexity.

### Thread model

```
Main thread (Tk event loop)
  └── root.after(200ms) → _tick()
        ├── _drain_q()       — consumes log_q from background thread
        ├── _update_duration()
        └── _poll_logfile()  — tails the debug .log file

Background thread (daemon)
  └── _stream_stdout()  — blocks on proc.stdout line-by-line, puts to log_q
```

All widget updates happen on the main thread. The background thread only puts to the queue. This is the correct tkinter threading pattern — widget methods are not thread-safe.

### How P&L is displayed in real time

`main.py` logs at two levels:
- **stdout (INFO)** — key events: position opened, profit milestones (every `$0.50`), trade closed summary.
- **File (DEBUG)** — every tick's P&L.

The UI uses both:
1. `_stream_stdout()` — reads the INFO stream and parses key events.
2. `_poll_logfile()` — after the bot creates its `logs/*.log` file, the UI finds the newest `.log` file and tails it every 200 ms, extracting `[DEBUG]` P&L lines with regex.

This means P&L updates ~every tick (~50 ms from the bot) but the UI displays at 200 ms polling resolution, which is fine for visual feedback.

### How the final trade summary is captured

The bot logs a single INFO line at close that contains all fields:
```
reason=profit_trailing duration_seconds=45 entry_price=2345.67 exit_price=2347.89
final_pnl=1.87 peak_profit=2.10 ...
```

`_handle_line()` matches this with:
```python
re.search(r"(?:exit_reason|reason)[=:\s]+(\w+).*?final_pnl[=:\s]+\$?([-\d.]+)", line, re.I)
```

When matched, `_finalize()` is called to record the trade, update history, and reset the UI.

### Existing-position auto-response

`main.py` calls `input()` when it detects existing open positions, asking whether to manage an existing position or open a new one. The UI's `_stream_stdout()` detects these prompts and auto-writes `"new\n"` to the subprocess stdin, since the UI is always opening new trades.

### SIGINT on close

Clicking **CLOSE TRADE** sends `signal.CTRL_C_EVENT` (Windows) or `SIGINT` (Linux/Mac) to the bot subprocess. The bot handles this as `reason="manual_shutdown"`, closes the MT5 position with retry logic, then exits cleanly. The UI detects process exit via the `"__DONE__"` sentinel from the background thread.

---

## Layout structure

```
root (Tk)
├── topbar              — title + account info + connection status
└── body (grid, 2 cols)
    ├── left (col 0, 370px fixed)
    │   ├── input card  — Symbol, Lots, BUY/SELL buttons
    │   └── advanced    — collapsible scrollable grid of all 17 params
    └── right (col 1, expands)
        ├── row 0: active trade card   — P&L, duration, indicators, CLOSE btn
        ├── row 1: stats bar           — last trade + today/all-time stats
        ├── row 2: history table       — sortable, expands vertically (weight=1)
        └── row 3: bot log             — scrolling text, color-coded by log level
```

The right panel uses `grid` with `rowconfigure(2, weight=1)` so the history table fills all available vertical space while the other rows stay at their natural height.

---

## State indicators

Four pill-shaped indicators light up as the bot progresses through trade phases:

| Indicator | Color | Triggered when |
|-----------|-------|----------------|
| WARMUP | Amber | Always on at trade start; off when warmup period ends |
| BREAKEVEN | Blue | Bot logs "breakeven stop activated" |
| TRAILING | Green | Bot logs "profit target activated" |
| PARTIAL | Purple | Bot logs "partial close" |

---

## Persistence

**`trade_history.json`** — array of trade objects. Each entry:
```json
{
  "symbol": "XAUUSD",
  "type": "buy",
  "lots": "0.01",
  "ticket": "12345678",
  "entry": "2345.67",
  "exit": "2347.89",
  "peak": 2.10,
  "open_time": "09:23:45",
  "open_ts": 1712484225.3,
  "close_time": "09:24:30",
  "close_ts": 1712484270.1,
  "date": "2026-04-06",
  "exit_reason": "profit_trailing",
  "pnl": 1.87,
  "duration": 45
}
```

**`ui_settings.json`** — flat dict of every field key → last used string value. Merged over `DEFAULTS` on startup so new fields added to `DEFAULTS` are automatically populated.

---

## Adding or changing parameters

If `main.py` adds a new CLI argument (e.g., `-new-param`):

1. Add to `DEFAULTS` dict in `ui.py`:
   ```python
   "new_param": "default_value",
   ```
2. Add to `ARG_MAP` dict in `ui.py`:
   ```python
   "new_param": "-new-param",
   ```
3. Add to the `params` list inside `_build_advanced()` with label and default:
   ```python
   ("new_param", "New Param Label", "default_value"),
   ```

No other changes needed. The field will appear in the Advanced Settings panel and be passed to the subprocess automatically.

---

## Log parsing — fragile points

The UI parses freeform log text with regex. If the bot's log message format changes, these patterns may need updating:

| What is parsed | Location in ui.py | Pattern |
|---------------|-------------------|---------|
| Ticket number | `_handle_line` | `ticket[=:\s#]+(\d+)` |
| Entry price | `_handle_line` | `open(?:ed)?\s+(?:price\|at)[=:\s]+([\d.]+)` |
| Final P&L summary | `_handle_line` | `(?:exit_reason\|reason)[=:\s]+(\w+).*?final_pnl[=:\s]+\$?([-\d.]+)` |
| Peak profit | `_handle_line` | `peak\s*profit[=:\s]+\$?([\d.]+)` |
| Tick-level P&L | `_poll_logfile` | `\[DEBUG\].*?pnl[:\s=]+\$?([-\d.]+)` |

---

## Agent Mode

### What it is

An optional LLM-driven layer (`agent.py`) that decides *when* to open a trade,
with *which* of the bot's existing 16 risk parameters, and — if enabled — when
to request an early close. It never touches `main.py` or its 50ms tick loop
directly; it only ever uses the same two levers a human has:

1. Launch `main.py` as a subprocess with a chosen `trade_type`/`lots`/config,
   via the exact same code path as the BUY/SELL buttons (`_open_trade`).
2. Write the same `.close_{magic}` stop-file the CLOSE TRADE button writes.

This keeps the latency-critical single-loop design in `main.py` completely
untouched — the agent is purely an outer decision-maker, never an inner one.

### Why a separate thread, not asyncio

`ui.py` has no event loop (it's tkinter). `AgentRunner` runs in its own daemon
thread, sleeping in 1-second increments between decision cycles (so toggling
it off stops it within a second, not at the end of a long sleep). Like
`_stream_stdout`, it never touches a Tk widget directly — it only ever puts
messages on the existing `log_q` queue (`AGENT_LOG`, `AGENT_OPEN`,
`AGENT_CLOSE`), which the Tk `_tick()` heartbeat drains on the main thread via
`_drain_q`.

### Backend: Claude Code CLI (headless)

v1 supports one backend, `ClaudeCodeBackend`, which shells out to:

```
claude -p "<prompt>" --allowedTools "" \
  --output-format json --json-schema '<AgentDecision schema>'
```

- No `--bare` — it looked appealing for a faster one-shot call (skips
  hooks/MCP/CLAUDE.md loading), but it also skips loading the stored
  credentials, so every call fails with "Not logged in · Please run /login"
  even on an already-authenticated machine. Confirmed by reproducing
  `claude --bare -p "say hi"` directly, which fails the same way outside
  this app. Left out entirely rather than worked around.
- `--allowedTools ""` means the model can only reason from the prompt text —
  it cannot read/write files or run tools. This is a pure decision call.
- `--json-schema` means the response comes back pre-validated in a
  `structured_output` field — no regex/fence-scraping of free-text JSON.
- A Python-side `subprocess.run(..., timeout=...)` backstop (in addition to
  `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`) means a hung CLI call can never
  stall the agent loop — it degrades to a logged "skip" instead.
- No API key handling needed for this backend; it relies on the user's
  existing `claude` auth. `AgentBackend` is an abstract base so an
  Anthropic/OpenAI HTTP backend could be added later behind the same
  `decide()` contract, without touching `AgentRunner`.

### What the agent can and can't do

- It can choose `trade_type`, `lots` (capped by the UI's "Max Lots" field and
  the symbol's real `volume_min`/`volume_max`/`volume_step`), and override any
  of the 16 optional `TradeConfig` fields (profit/loss/trailing/velocity/etc.)
  — but every override is clamped to a hardcoded safety bounds table
  (`CONFIG_BOUNDS` in `agent.py`) before use, so a bad or hallucinated value
  (e.g. negative `loss_max`) can never reach a real trade.
- It can request an early close (if "Allow agent to request early close" is
  checked) — same stop-file mechanism as the manual button.
- It cannot change `main.py`'s exit-priority logic, bypass `hard_stop`, run
  more than one trade at a time, or touch a trade after it's already been
  handed off to `main.py` except through that one early-close request.
- Turning the toggle off stops new decisions within ~1s; it does not touch an
  already-open trade.

### Guardrails checked every cycle, before any LLM call

1. Enabled? (toggle)
2. Within the configured daily time window? (handles windows that span
   midnight)
3. Daily P&L guardrails: paused for the rest of the day if today's realized
   P&L (from `trade_history.json`, same aggregation `_refresh_stats` already
   does) has hit either the daily loss limit or the daily profit target.

Only if all three pass does it spend a `claude` CLI call. This means a
misconfigured or paused agent costs nothing.

### Pulling market context

`agent.fetch_market_context()` connects to MT5 directly from the UI process
(separately from `main.py`'s own connection — these are independent OS
processes, so this is safe) and summarizes the last hour as 1-minute OHLC
bars plus current bid/ask/spread, rather than dumping raw ticks — an hour of
raw XAUUSD ticks is tens of thousands of points and would blow the prompt
token budget for no benefit over an OHLC summary.

### Learning from its own past sessions

Every decision and its eventual outcome are appended to
`logs/agent/agent_YYYYMMDD.jsonl` (`AgentLogger.record_decision` /
`record_outcome`), linked by timestamp. Before every prompt,
`load_recent_agent_history()` reads back the last few decision→outcome pairs
(across all `agent_*.jsonl` files, so this persists across UI restarts) and
renders a short summary — e.g.
`Opened buy 0.02 lots (momentum thesis) -> closed hard_stop pnl=-$5.00`
— which gets injected into the next prompt so the model can see whether its
recent theses actually worked out.

### Why the agent's log is separate from the bot log

The existing "BOT LOG" panel shows `main.py`'s own stdout (INFO-level
execution events). The new "AGENT LOG" panel is fed independently via the
`AGENT_LOG` queue message and shows only the agent's decisions and reasoning
— "why did the agent do that, right or wrong" is a different question from
"what did the bot's tick loop do," and mixing them would make both harder to
read.

### Adding a new agent backend later

Implement `agent.AgentBackend.decide(prompt, schema, model) -> AgentDecision`
and pass an instance to `AgentRunner(backend=...)` in `ui.py`'s
`Dashboard.__init__`. Nothing else needs to change — `AgentRunner` only ever
calls `self.backend.decide(...)`.

## Known limitations

- **No live bid/ask price display for manual trading** — the manual trade path in the UI does not connect to MT5 directly; it only reads the bot's log output. (Agent Mode is the exception: `agent.py` does open its own direct MT5 connection, but only to summarize the last hour of price action for the LLM prompt, not to drive a live price display.)
- **P&L resolution** — from stdout, P&L updates come at peak-profit milestones (`$0.50` increments). Tick-level updates come from log file tailing at 200 ms polling. There is no sub-200 ms P&L display.
- **Single active trade** — the UI manages one bot process at a time. To run multiple simultaneous trades (different magic numbers), multiple UI instances would be needed. Agent Mode inherits this: it will not open a new trade while one is already active.
- **Windows-only** — same constraint as `main.py` due to the MetaTrader5 library. The `CTRL_C_EVENT` signal path in `_close_trade()` is Windows-specific; Linux/Mac uses `SIGINT` via the else branch.
- **Agent Mode requires the `claude` CLI** — `ClaudeCodeBackend` shells out to `claude`; if it isn't installed/authenticated/on PATH, every decision cycle logs a "skip" with the failure reason rather than trading blind. Only one backend (Claude Code CLI) ships in v1; see "Adding a new agent backend later" above for extending it.
- **Agent Mode decision cadence is coarse by design** — a `claude` CLI call takes real seconds, so `decision_interval_s`/`monitor_interval_s` default to 60s/30s (minimum 15s each). It is not a per-tick decision-maker; `main.py`'s own exit logic remains the only per-tick safety net.
