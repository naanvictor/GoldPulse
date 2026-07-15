# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**GoldPulse** — an MT5 XAUUSD scalping bot, optionally powered by Claude Code for agentic trade decisions. A high-speed trading bot for MetaTrader 5 targeting volatile instruments (primarily XAUUSD). Single-file Python application (`main.py`) using asyncio for concurrency; the full architecture spec lives in `README.md`. A companion dashboard (`ui.py`) and optional agentic layer (`agent.py`) are documented in `UI_README.md`.

## Tech Stack & Environment

- Python 3.14 (see `.python-version`)
- Package manager: uv (inferred from `pyproject.toml` format and `.python-version`)
- Single external dependency: `MetaTrader5` (Windows-only, synchronous API)
- No test framework configured yet

## Build & Run Commands

```bash
uv run python main.py <lots> -symbol <SYMBOL> -type <buy|sell> [options]
```

No build step, linter, or test suite currently configured.

## Architecture (from README.md spec)

### Core Design Constraints

1. **All MT5 calls must be wrapped with `asyncio.to_thread()`** — the MetaTrader5 library is fully synchronous and will block the event loop otherwise.
2. **Single-loop design** — tick collection and exit evaluation happen in the same loop iteration. Do NOT separate into independent loops; the latency penalty (`$2-5` on XAUUSD spikes) is unacceptable for scalping.
3. **Single file** — all code lives in `main.py`.

### Async Structure (2 coroutines via `asyncio.gather()`)

- **`trade_loop(config, trade_state, tick_buffer, stop_event)`**: Tight ~50ms loop that polls ticks, stores them in a deque, and immediately evaluates all exit conditions against each new tick. Also handles tick drought detection and periodic position existence checks.
- **`run()` (main coroutine)**: CLI parsing → validation → spread gate → position open → entry slippage check → launches trade_loop → close with retry → close verification → final summary.

### Key Data Structures

- `TradeConfig` (dataclass): All CLI parameters — see README for full field list and defaults.
- `TradeState` (dataclass): Mutable trade state (ticket, P&L tracking, peak profit, velocities, slippage, timing).
- `TickEntry` (namedtuple): `(timestamp, bid, ask)` stored in a `collections.deque(maxlen=6000)`.

### Exit Logic Priority (highest to lowest)

`hard_stop` > `max_duration` > `loss_velocity` > `breakeven_stop` > `profit_trailing` > `profit_evaporated`

- Hard stop and loss velocity are active from second 0 (never suppressed by warmup).
- Spread exceeding `max_spread` skips all exit evaluation EXCEPT hard stop.
- Profit trailing uses the tighter of percentage-based and dollar-based thresholds.

### Loss Velocity Detection

Two modes (either triggers exit): directional velocity (start-to-end of window) and peak drawdown within window. Uses a pre-computed `pnl_per_point` ratio for performance instead of calling `order_calc_profit` for every historical tick.

### Position Close

MT5 has no direct close — send an opposite order. Retry strategy: 3 attempts with escalating deviation (+10 points/retry), then emergency close with `emergency_deviation` and `ORDER_FILLING_RETURN`. Always verify close via `positions_get()` afterward.

### Disconnect Handling

Exponential backoff reconnect (5 attempts: 1s, 2s, 4s, 8s, 16s). All time-based calculations subtract `total_disconnect_seconds` to prevent disconnect gaps from contaminating warmup/duration/velocity logic.

### Signal Handling

Windows: `signal.signal()` with `loop.call_soon_threadsafe()` (no `add_signal_handler` support). Linux/Mac: `loop.add_signal_handler()`.

## Important Notes

- `main.py` currently contains hardcoded credentials — these must be removed before any commit to a remote repository.
- P&L calculation uses `mt5.order_calc_profit()` for accuracy. After partial close, use `remaining_lots` not `config.lots`.
- Logging: console at INFO, file at DEBUG under `logs/` directory.
