#!/usr/bin/env python
"""Agentic mode for GoldPulse, an XAUUSD scalping bot UI optionally powered
by Claude Code for agentic trade decisions.

An LLM-driven layer that decides when to open trades (and, optionally, when
to request an early close) based on recent price action, the bot's existing
risk parameters, and the outcome of its own past decisions.

This module runs entirely inside the UI process, in its own background
thread (see AgentRunner). It never talks to main.py's tight tick loop
directly -- its only two levers are the same ones a human has:
  1. Launch main.py as a subprocess with a chosen trade_type/lots/config,
     exactly like the BUY/SELL buttons (via a callback into the UI).
  2. Write the same .close_{magic} stop-file the CLOSE TRADE button writes.

Decision logs are kept entirely separate from main.py's execution logs
(logs/{symbol}_{type}_*.log) under logs/agent/, so "why did the agent do
that" never gets mixed up with "what did the bot do tick-by-tick".
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Callable, Optional

import MetaTrader5 as mt5

AGENT_LOG_DIR = Path(__file__).parent / "logs" / "agent"
AGENT_SETTINGS_FILE = Path(__file__).parent / "agent_settings.json"

DEFAULT_PROMPT = (
    "You are a cautious risk-manager for GoldPulse, a XAUUSD scalping bot. Your job is to "
    "decide whether current market conditions favor a short-duration scalp "
    "trade right now -- not to chase every move. Prefer skipping when the "
    "spread is wide, price action is choppy or unclear, or recent agent "
    "sessions show repeated losses on similar setups. When you do open a "
    "trade, prefer tighter risk parameters shortly after a loss, and you may "
    "loosen them slightly after a clean win."
)

# Fields the agent is allowed to override, mirroring main.py's TradeConfig
# optional fields (symbol/trade_type/lots/magic are handled separately, not
# part of the free-form override dict). Each entry: (min, max, is_int).
CONFIG_BOUNDS: dict[str, tuple[float, float, bool]] = {
    "profit_min":           (0.10, 20.0, False),
    "loss_max":             (0.50, 50.0, False),
    "profit_trail_pct":     (1.0, 90.0, False),
    "profit_trail_amount":  (0.05, 10.0, False),
    "loss_velocity":        (0.20, 30.0, False),
    "loss_velocity_window": (5, 120, True),
    "max_spread":           (5, 200, True),
    "warmup":               (0, 30, True),
    "max_duration":         (10, 3600, True),
    "breakeven_at":         (0.10, 20.0, False),
    "breakeven_buffer":     (0.0, 10.0, False),
    "partial_close_pct":    (0, 90, True),
    "max_entry_slippage":   (1, 200, True),
    "max_exit_slippage":    (1, 500, True),
    "deviation":            (1, 200, True),
    "emergency_deviation":  (1, 500, True),
}

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["open", "skip", "close", "hold"]},
        "trade_type": {"type": "string", "enum": ["buy", "sell", "none"]},
        "lots": {"type": "number"},
        "config_overrides": {
            "type": "object",
            "additionalProperties": {"type": "number"},
        },
        "reasoning": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["action", "reasoning"],
}


# ═══════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class AgentConfig:
    """Everything the UI's Agent Mode panel controls, adjustable at runtime."""

    enabled: bool = False
    prompt: str = DEFAULT_PROMPT
    model: str = ""                  # "" = let the claude CLI pick its default
    start_time: str = "00:00"        # daily window, HH:MM
    end_time: str = "23:59"
    daily_loss_limit: float = 10.0   # pause agent for the day if breached
    daily_profit_target: float = 20.0
    max_lots: float = 0.05
    decision_interval_s: int = 60    # how often it evaluates opening a trade
    monitor_interval_s: int = 30     # how often it re-evaluates an open trade
    allow_early_close: bool = True


def load_agent_config() -> AgentConfig:
    cfg = AgentConfig()
    if AGENT_SETTINGS_FILE.exists():
        try:
            with open(AGENT_SETTINGS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            for key, value in data.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
        except Exception:
            pass
    return cfg


def save_agent_config(cfg: AgentConfig) -> None:
    with open(AGENT_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg.__dict__, f, indent=2)


# ═══════════════════════════════════════════════════════════════════════════
# Decision + backends
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class AgentDecision:
    action: str = "skip"
    trade_type: str = "none"
    lots: float = 0.0
    config_overrides: dict = field(default_factory=dict)
    reasoning: str = ""
    confidence: Optional[float] = None
    raw: dict = field(default_factory=dict)


class AgentBackend:
    """Abstract decision backend. ClaudeCodeBackend is the only v1 impl; an
    AnthropicAPIBackend/OpenAICompatibleBackend can implement this same
    contract later without touching AgentRunner."""

    def decide(self, prompt: str, schema: dict, model: str = "") -> AgentDecision:
        raise NotImplementedError


def _claude_command(args: list[str]) -> list[str]:
    """Resolve the claude CLI, handling the common case where it's an npm
    .cmd/.bat shim on Windows (CreateProcess can't exec those directly)."""
    exe = shutil.which("claude") or "claude"
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe, *args]
    return [exe, *args]


class ClaudeCodeBackend(AgentBackend):
    """Shells out to `claude` in headless/print mode with a JSON Schema, so
    the response comes back as pre-validated structured output -- no
    free-text JSON scraping needed."""

    def __init__(self, timeout_s: int = 45):
        self.timeout_s = timeout_s

    def decide(self, prompt: str, schema: dict, model: str = "") -> AgentDecision:
        args = [
            "-p", prompt,
            "--allowedTools", "",
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
        ]
        if model:
            args += ["--model", model]
        cmd = _claude_command(args)

        env = dict(os.environ)
        env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = str(self.timeout_s * 1000)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_s + 5,
                env=env,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                ),
            )
        except subprocess.TimeoutExpired:
            return AgentDecision(action="skip", reasoning="claude CLI call timed out")
        except FileNotFoundError:
            return AgentDecision(
                action="skip", reasoning="claude CLI not found on PATH"
            )

        if result.returncode != 0:
            # On failure the CLI still emits a JSON envelope on stdout (e.g.
            # {"is_error": true, "result": "Not logged in - Please run
            # /login", ...}) -- that's the actionable message. stderr is
            # often just a benign warning (e.g. "no stdin data received"),
            # so only fall back to it if stdout has no such envelope.
            detail = None
            if result.stdout.strip():
                try:
                    envelope = json.loads(result.stdout)
                    detail = envelope.get("result") or envelope.get("subtype")
                except json.JSONDecodeError:
                    pass
            if not detail:
                detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
            return AgentDecision(
                action="skip",
                reasoning=f"claude CLI exited {result.returncode}: {str(detail)[:500]}",
            )

        try:
            envelope = json.loads(result.stdout)
            structured = envelope.get("structured_output")
            if structured is None:
                return AgentDecision(
                    action="skip",
                    reasoning=f"no structured_output in response: {result.stdout[:500]}",
                )
            return AgentDecision(
                action=structured.get("action", "skip"),
                trade_type=structured.get("trade_type", "none"),
                lots=float(structured.get("lots") or 0.0),
                config_overrides=structured.get("config_overrides") or {},
                reasoning=structured.get("reasoning", ""),
                confidence=structured.get("confidence"),
                raw=envelope,
            )
        except (json.JSONDecodeError, ValueError, AttributeError, TypeError) as e:
            return AgentDecision(
                action="skip",
                reasoning=f"failed to parse claude response: {e}: {result.stdout[:500]}",
            )


def _clamp_overrides(overrides: dict) -> dict:
    """Restrict to known TradeConfig fields and clamp to a safe bounds table
    so a bad/hallucinated model output can never slip an unsafe value into a
    real trade (e.g. a negative loss_max)."""
    clamped: dict = {}
    for key, value in (overrides or {}).items():
        bounds = CONFIG_BOUNDS.get(key)
        if bounds is None:
            continue
        lo, hi, is_int = bounds
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        v = max(lo, min(hi, v))
        clamped[key] = int(round(v)) if is_int else round(v, 4)
    return clamped


# ═══════════════════════════════════════════════════════════════════════════
# Market context
# ═══════════════════════════════════════════════════════════════════════════

def fetch_market_context(symbol: str) -> str:
    """Pull the last hour of price action for `symbol` and render a compact
    text summary for the prompt. Uses 1-minute bars rather than raw ticks --
    an hour of raw XAUUSD ticks is tens of thousands of points and would blow
    the prompt budget for no real benefit over an OHLC summary."""
    if not mt5.initialize():
        return f"(MT5 unavailable: {mt5.last_error()})"

    bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 60)
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    if bars is None or len(bars) == 0 or tick is None or info is None:
        return f"(no market data available for {symbol})"

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    first, last = closes[0], closes[-1]
    change = last - first
    change_pct = (change / first * 100) if first else 0.0
    rng = max(highs) - min(lows)
    spread_pts = (tick.ask - tick.bid) / info.point if info.point else 0.0

    return (
        f"{symbol} last {len(bars)} minutes: "
        f"open={first:.2f} last={last:.2f} change={change:+.2f} ({change_pct:+.2f}%) "
        f"range={rng:.2f} high={max(highs):.2f} low={min(lows):.2f} "
        f"current_bid={tick.bid} current_ask={tick.ask} "
        f"current_spread_pts={spread_pts:.0f}"
    )


def get_lot_constraints(symbol: str) -> Optional[tuple[float, float, float]]:
    """Return (volume_min, volume_max, volume_step) for symbol, or None."""
    if not mt5.initialize():
        return None
    info = mt5.symbol_info(symbol)
    if info is None:
        return None
    return info.volume_min, info.volume_max, info.volume_step


# ═══════════════════════════════════════════════════════════════════════════
# Logging + cross-session self-review
# ═══════════════════════════════════════════════════════════════════════════

class AgentLogger:
    """Writes agent decisions/outcomes to logs/agent/, entirely separate from
    main.py's execution logs and from the UI's own BOT LOG panel."""

    def __init__(self):
        AGENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y%m%d")
        self.jsonl_path = AGENT_LOG_DIR / f"agent_{today}.jsonl"
        self.log_path = AGENT_LOG_DIR / f"agent_{today}.log"

    def _write_line(self, level: str, message: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(f"{ts} [{level}] {message}\n")

    def info(self, message: str) -> None:
        self._write_line("INFO", message)

    def warning(self, message: str) -> None:
        self._write_line("WARNING", message)

    def record_decision(
        self, context: str, decision: AgentDecision, guardrail: Optional[str] = None
    ) -> dict:
        record = {
            "ts": time.time(),
            "type": "decision",
            "context": context,
            "guardrail": guardrail,
            "action": decision.action,
            "trade_type": decision.trade_type,
            "lots": decision.lots,
            "config_overrides": decision.config_overrides,
            "reasoning": decision.reasoning,
            "confidence": decision.confidence,
        }
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        self.info(f"decision={decision.action} reasoning={decision.reasoning}")
        return record

    def record_outcome(self, decision_ts: Optional[float], trade: dict) -> None:
        record = {
            "ts": time.time(),
            "type": "outcome",
            "decision_ts": decision_ts,
            "ticket": trade.get("ticket"),
            "pnl": trade.get("pnl"),
            "exit_reason": trade.get("exit_reason"),
            "duration": trade.get("duration"),
        }
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        self.info(
            f"outcome ticket={trade.get('ticket')} pnl={trade.get('pnl')} "
            f"reason={trade.get('exit_reason')}"
        )


def load_recent_agent_history(n_sessions: int = 3) -> str:
    """Summarize the last few decision+outcome pairs across all agent log
    files (most recent first) for injection into the next prompt -- this is
    the "learn from the last run's mistakes" input."""
    if not AGENT_LOG_DIR.exists():
        return "(no prior agent history)"

    files = sorted(AGENT_LOG_DIR.glob("agent_*.jsonl"), reverse=True)
    records: list[dict] = []
    for fp in files:
        try:
            with open(fp, encoding="utf-8") as f:
                records.extend(json.loads(line) for line in f if line.strip())
        except Exception:
            continue
        if len(records) > 500:
            break

    decisions = {
        r["ts"]: r
        for r in records
        if r.get("type") == "decision" and r.get("action") == "open"
    }
    outcomes = [r for r in records if r.get("type") == "outcome"]

    lines = []
    for outcome in sorted(outcomes, key=lambda r: r["ts"], reverse=True)[:n_sessions]:
        decision = decisions.get(outcome.get("decision_ts"))
        if decision is None:
            continue
        pnl = outcome.get("pnl")
        pnl_str = f"${pnl:+.2f}" if isinstance(pnl, (int, float)) else "unknown"
        lines.append(
            f"- Opened {decision.get('trade_type')} {decision.get('lots')} lots "
            f"(\"{decision.get('reasoning', '')[:160]}\") -> "
            f"closed {outcome.get('exit_reason')} pnl={pnl_str}"
        )

    if not lines:
        return "(no prior agent trade outcomes yet)"
    return "Recent agent session outcomes (most recent first):\n" + "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════

class AgentRunner:
    """Background orchestration loop for agentic mode.

    All MT5/CLI/file work happens on this thread. The only way this class
    talks to the Tk UI is through the callbacks passed in, which themselves
    only ever enqueue work for the main-thread heartbeat to execute -- this
    thread never touches a Tk widget directly.
    """

    def __init__(
        self,
        get_config: Callable[[], AgentConfig],
        get_symbol: Callable[[], str],
        get_baseline_settings: Callable[[], dict],
        is_trade_active: Callable[[], bool],
        get_trade_history: Callable[[], list],
        request_open_trade: Callable[[str, str, dict], None],
        request_close_trade: Callable[[], None],
        ui_log: Callable[[str], None],
        backend: Optional[AgentBackend] = None,
    ):
        self._get_config = get_config
        self._get_symbol = get_symbol
        self._get_baseline_settings = get_baseline_settings
        self._is_trade_active = is_trade_active
        self._get_trade_history = get_trade_history
        self._request_open_trade = request_open_trade
        self._request_close_trade = request_close_trade
        self._ui_log = ui_log
        self.backend = backend or ClaudeCodeBackend()

        self.logger = AgentLogger()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.state = "off"  # off | idle | thinking | trading | paused | off_hours

        self._awaiting_outcome = False
        self._pending_decision_ts: Optional[float] = None
        self._last_history_len = len(get_trade_history())

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ─── main loop ──────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            # Everything in this iteration -- including reading config back
            # out for the sleep interval -- must stay inside this try/except.
            # A single uncaught exception here would silently kill this daemon
            # thread, freezing self.state forever (e.g. stuck on "idle") with
            # no further retries and no visible error.
            try:
                self._cycle()
                cfg = self._get_config()
                interval = (
                    cfg.monitor_interval_s
                    if self._is_trade_active()
                    else cfg.decision_interval_s
                )
                interval = max(5, interval)
            except Exception as e:
                self.logger.warning(f"agent cycle error: {e}")
                self._ui_log(f"[AGENT] cycle error: {e}")
                interval = 5  # brief backoff, then retry rather than dying

            slept = 0.0
            while slept < interval and not self._stop.is_set():
                time.sleep(1.0)
                slept += 1.0

    def _cycle(self) -> None:
        cfg = self._get_config()

        # Track outcomes of trades we opened regardless of enabled state, so
        # history stays accurate even if the user disables mid-trade.
        self._check_for_outcome()

        if not cfg.enabled:
            self.state = "off"
            return

        if not self._within_time_window(cfg):
            self.state = "off_hours"
            return

        guardrail = self._check_pnl_guardrails(cfg)
        if guardrail:
            self.state = "paused"
            self.logger.info(f"guardrail: {guardrail}, skipping cycle")
            return

        if self._is_trade_active():
            if cfg.allow_early_close:
                self.state = "trading"
                self._evaluate_close(cfg)
            return

        self.state = "thinking"
        self._evaluate_open(cfg)
        self.state = "idle"

    # ─── guardrails ─────────────────────────────────────────────────────

    def _within_time_window(self, cfg: AgentConfig) -> bool:
        try:
            start = dtime.fromisoformat(cfg.start_time)
            end = dtime.fromisoformat(cfg.end_time)
        except ValueError:
            return True  # malformed window -> fail open, don't silently block
        now = datetime.now().time()
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end  # window spans midnight

    def _todays_pnl(self) -> float:
        today = date.today().isoformat()
        return sum(
            t["pnl"]
            for t in self._get_trade_history()
            if t.get("date") == today and isinstance(t.get("pnl"), (int, float))
        )

    def _check_pnl_guardrails(self, cfg: AgentConfig) -> Optional[str]:
        pnl = self._todays_pnl()
        if pnl <= -abs(cfg.daily_loss_limit):
            return f"daily loss limit reached (${pnl:.2f} <= -${cfg.daily_loss_limit:.2f})"
        if pnl >= abs(cfg.daily_profit_target):
            return f"daily profit target reached (${pnl:.2f} >= ${cfg.daily_profit_target:.2f})"
        return None

    # ─── decisions ──────────────────────────────────────────────────────

    def _build_prompt(self, cfg: AgentConfig, symbol: str, mode: str) -> str:
        market = fetch_market_context(symbol)
        history_summary = load_recent_agent_history()
        baseline = self._get_baseline_settings()
        baseline_str = ", ".join(
            f"{k}={v}" for k, v in baseline.items() if k in CONFIG_BOUNDS
        )

        if mode == "open":
            task = (
                "Decide whether to open a new trade right now. If yes, set "
                'action="open", trade_type to "buy" or "sell", lots (a small '
                f'number, e.g. between the symbol minimum and {cfg.max_lots}), '
                "and optionally config_overrides for any of the baseline risk "
                "parameters you want to tighten or loosen for this trade. If "
                'conditions are unclear or unfavorable, set action="skip".'
            )
        else:
            task = (
                "A trade is currently open. Decide whether to request an "
                'early close. Set action="close" to close now, or '
                'action="hold" to let the bot\'s own exit logic keep '
                "managing it."
            )

        return (
            f"{cfg.prompt.strip()}\n\n"
            f"--- Task ---\n{task}\n\n"
            f"--- Market context ---\n{market}\n\n"
            f"--- Baseline risk parameters (override only what you want to change) ---\n"
            f"{baseline_str}\n\n"
            f"--- {history_summary} ---\n\n"
            "Respond only with the structured decision fields; keep `reasoning` "
            "to 2-3 sentences explaining the market read and why."
        )

    def _evaluate_open(self, cfg: AgentConfig) -> None:
        symbol = self._get_symbol()
        prompt = self._build_prompt(cfg, symbol, mode="open")
        decision = self.backend.decide(prompt, DECISION_SCHEMA, model=cfg.model)
        record = self.logger.record_decision(f"open-eval:{symbol}", decision)
        self._ui_log(f"[AGENT] {decision.action}: {decision.reasoning}")

        if decision.action != "open":
            return

        trade_type = decision.trade_type if decision.trade_type in ("buy", "sell") else None
        if trade_type is None:
            self.logger.warning(
                f"decision action=open but trade_type invalid: "
                f"{decision.trade_type!r}, skipping"
            )
            return

        lots = decision.lots if decision.lots and decision.lots > 0 else 0.01
        lots = min(lots, cfg.max_lots)
        constraints = get_lot_constraints(symbol)
        if constraints:
            vmin, vmax, vstep = constraints
            lots = max(vmin, min(vmax, lots))
            if vstep:
                steps = round((lots - vmin) / vstep)
                lots = round(vmin + steps * vstep, 4)

        overrides = _clamp_overrides(decision.config_overrides)

        self._pending_decision_ts = record["ts"]
        self._awaiting_outcome = True
        self._last_history_len = len(self._get_trade_history())
        self._request_open_trade(trade_type, f"{lots:.4f}".rstrip("0").rstrip("."), overrides)

    def _evaluate_close(self, cfg: AgentConfig) -> None:
        symbol = self._get_symbol()
        prompt = self._build_prompt(cfg, symbol, mode="monitor")
        decision = self.backend.decide(prompt, DECISION_SCHEMA, model=cfg.model)
        self.logger.record_decision(f"monitor-eval:{symbol}", decision)
        self._ui_log(f"[AGENT] {decision.action}: {decision.reasoning}")

        if decision.action == "close":
            self._request_close_trade()

    def _check_for_outcome(self) -> None:
        if not self._awaiting_outcome:
            return
        history = self._get_trade_history()
        if len(history) > self._last_history_len:
            trade = history[-1]
            self.logger.record_outcome(self._pending_decision_ts, trade)
            self._awaiting_outcome = False
            self._pending_decision_ts = None
            self._last_history_len = len(history)
