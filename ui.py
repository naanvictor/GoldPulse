#!/usr/bin/env python
"""
GoldPulse — Trading Dashboard
An XAUUSD scalping bot for MT5, optionally powered by Claude Code for
agentic trade decisions. Dark-themed GUI for fast trade placement, live
monitoring, and history.
"""

import json
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from tkinter import *
from tkinter import messagebox, ttk

import agent

# ═══════════════════════════════════════════════════════════════════════════════
# THEME — dark trading terminal
# ═══════════════════════════════════════════════════════════════════════════════
BG          = "#0d0d0f"
PANEL       = "#141418"
CARD        = "#1c1c22"
BORDER      = "#2a2a32"
BUY         = "#00cc88"
SELL        = "#e04444"
TEXT        = "#dde0e8"
MUTED       = "#5a5e6e"
PROFIT      = "#00ff88"
LOSS        = "#ff4444"
ACCENT      = "#f0a030"
WHITE       = "#ffffff"
IND_OFF_BG  = "#252530"
IND_OFF_FG  = "#44465a"

FONT_MONO   = "Consolas"

HISTORY_FILE = Path(__file__).parent / "trade_history.json"
SETTINGS_FILE = Path(__file__).parent / "ui_settings.json"

DEFAULTS = {
    "symbol":               "XAUUSD",
    "lots":                 "0.01",
    "profit_min":           "1.50",
    "loss_max":             "5.0",
    "profit_trail_pct":     "15.0",
    "profit_trail_amount":  "0.40",
    "loss_velocity":        "3.0",
    "loss_velocity_window": "30",
    "max_spread":           "30",
    "warmup":               "3",
    "max_duration":         "300",
    "breakeven_at":         "1.00",
    "breakeven_buffer":     "0.20",
    "partial_close_pct":    "0",
    "max_entry_slippage":   "30",
    "max_exit_slippage":    "100",
    "deviation":            "30",
    "emergency_deviation":  "100",
    "magic":                "234567",
}

ARG_MAP = {
    "profit_min":           "-profit-min",
    "loss_max":             "-loss-max",
    "profit_trail_pct":     "-profit-trail-pct",
    "profit_trail_amount":  "-profit-trail-amount",
    "loss_velocity":        "-loss-velocity",
    "loss_velocity_window": "-loss-velocity-window",
    "max_spread":           "-max-spread",
    "warmup":               "-warmup",
    "max_duration":         "-max-duration",
    "breakeven_at":         "-breakeven-at",
    "breakeven_buffer":     "-breakeven-buffer",
    "partial_close_pct":    "-partial-close-pct",
    "max_entry_slippage":   "-max-entry-slippage",
    "max_exit_slippage":    "-max-exit-slippage",
    "deviation":            "-deviation",
    "emergency_deviation":  "-emergency-deviation",
    "magic":                "-magic",
}

# ═══════════════════════════════════════════════════════════════════════════════
# Persistence
# ═══════════════════════════════════════════════════════════════════════════════

def _load(path: Path, default):
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _save(path: Path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# Dashboard
# ═══════════════════════════════════════════════════════════════════════════════

class Dashboard:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("GoldPulse")
        self.root.configure(bg=BG)
        self.root.geometry("1260x870")
        self.root.minsize(960, 720)

        # Persisted data
        self.settings: dict = {**DEFAULTS, **_load(SETTINGS_FILE, {})}
        self.history:  list = _load(HISTORY_FILE, [])

        # Runtime state
        self.bot_proc:      subprocess.Popen | None = None
        self.log_q:         queue.Queue = queue.Queue()
        self.active:        dict | None = None
        self.live_pnl:      float = 0.0
        self.live_peak:     float = 0.0
        self.live_maxloss:  float = 0.0
        self.log_file_path: Path | None = None
        self.log_file_pos:  int = 0
        self.vars:          dict[str, StringVar] = {}
        # Trade summary buffer — populated line-by-line from the multiline
        # "=== TRADE SUMMARY ===" block the bot logs at close.
        self._in_summary:   bool = False
        self._summary_buf:  dict = {}

        self._build_ui()

        # Thread-safe snapshots for the agent's background thread to read.
        # Tkinter widgets (StringVar.get(), Text.get()) are not safe to call
        # from a non-main thread -- doing so directly was the root cause of
        # the agent thread occasionally dying on a cross-thread Tcl error.
        # These snapshots are refreshed on the main thread inside _tick()
        # and are plain Python objects, safe for the agent thread to read.
        self._agent_config_snapshot = self._current_agent_config()
        self._agent_symbol_snapshot = self._current_agent_symbol()
        self._agent_baseline_snapshot = self._current_agent_baseline()

        self.agent_runner = agent.AgentRunner(
            get_config=lambda: self._agent_config_snapshot,
            get_symbol=lambda: self._agent_symbol_snapshot,
            get_baseline_settings=lambda: self._agent_baseline_snapshot,
            is_trade_active=lambda: self.bot_proc is not None and self.bot_proc.poll() is None,
            get_trade_history=lambda: self.history,
            request_open_trade=lambda tt, lots, ov: self.log_q.put(("AGENT_OPEN", (tt, lots, ov))),
            request_close_trade=lambda: self.log_q.put(("AGENT_CLOSE", None)),
            ui_log=lambda msg: self.log_q.put(("AGENT_LOG", msg)),
        )
        if self.agent_vars["enabled"].get():
            self.agent_runner.start()

        self._refresh_table()
        self._refresh_stats()
        self._tick()

    # ─── Layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Top bar
        self._build_topbar()

        # Body — two columns
        body = Frame(self.root, bg=BG)
        body.pack(fill=BOTH, expand=True, padx=8, pady=(0, 8))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # Left column (fixed width)
        left = Frame(body, bg=BG, width=370)
        left.grid(row=0, column=0, sticky=NS, padx=(0, 6))
        left.grid_propagate(False)
        self._build_left(left)

        # Right column (expands)
        right = Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky=NSEW)
        right.rowconfigure(2, weight=1)   # history expands
        right.columnconfigure(0, weight=1)
        self._build_right(right)

    def _build_topbar(self):
        bar = Frame(self.root, bg=PANEL)
        bar.pack(fill=X, padx=8, pady=(8, 6))

        Label(bar, text="⬡  GOLDPULSE",
              bg=PANEL, fg=ACCENT,
              font=(FONT_MONO, 12, "bold"), padx=12, pady=7).pack(side=LEFT)

        self._conn_lbl = Label(bar, text="● OFFLINE",
                                bg=PANEL, fg=MUTED,
                                font=(FONT_MONO, 9), padx=12)
        self._conn_lbl.pack(side=RIGHT)

        self._acct_lbl = Label(bar, text="",
                                bg=PANEL, fg=MUTED,
                                font=(FONT_MONO, 9), padx=12)
        self._acct_lbl.pack(side=RIGHT)

    def _build_left(self, parent):
        """Trade entry: symbol/lots + BUY/SELL + advanced settings."""
        # ── Inputs ──
        input_outer = Frame(parent, bg=BORDER)
        input_outer.pack(fill=X, pady=(0, 6))
        input_card = Frame(input_outer, bg=CARD)
        input_card.pack(fill=BOTH, padx=1, pady=1)

        hdr = Frame(input_card, bg=CARD)
        hdr.pack(fill=X, padx=10, pady=(8, 6))
        Label(hdr, text="SYMBOL", bg=CARD, fg=MUTED,
              font=(FONT_MONO, 7, "bold")).grid(row=0, column=0, sticky=W)
        Label(hdr, text="LOTS", bg=CARD, fg=MUTED,
              font=(FONT_MONO, 7, "bold")).grid(
            row=0, column=1, sticky=W, padx=(18, 0))

        sym_v = StringVar(value=self.settings.get("symbol", "XAUUSD"))
        self.vars["symbol"] = sym_v
        Entry(hdr, textvariable=sym_v, width=9,
              bg=PANEL, fg=WHITE, insertbackground=WHITE,
              font=(FONT_MONO, 15, "bold"), relief=FLAT, bd=4
              ).grid(row=1, column=0, sticky=W)

        lots_v = StringVar(value=self.settings.get("lots", "0.01"))
        self.vars["lots"] = lots_v
        Entry(hdr, textvariable=lots_v, width=8,
              bg=PANEL, fg=WHITE, insertbackground=WHITE,
              font=(FONT_MONO, 15, "bold"), relief=FLAT, bd=4
              ).grid(row=1, column=1, sticky=W, padx=(18, 0))

        # ── BUY / SELL ──
        btn_row = Frame(input_card, bg=CARD)
        btn_row.pack(fill=X, padx=10, pady=(2, 10))
        btn_row.columnconfigure(0, weight=1)
        btn_row.columnconfigure(1, weight=1)

        self.buy_btn = Button(
            btn_row, text="▲  BUY",
            bg=BUY, fg="#000000",
            font=(FONT_MONO, 17, "bold"), relief=FLAT, bd=0,
            cursor="hand2", activebackground="#00a870",
            command=lambda: self._open_trade("buy"))
        self.buy_btn.grid(row=0, column=0, sticky=EW, padx=(0, 3), ipady=18)

        self.sell_btn = Button(
            btn_row, text="▼  SELL",
            bg=SELL, fg=WHITE,
            font=(FONT_MONO, 17, "bold"), relief=FLAT, bd=0,
            cursor="hand2", activebackground="#b83030",
            command=lambda: self._open_trade("sell"))
        self.sell_btn.grid(row=0, column=1, sticky=EW, padx=(3, 0), ipady=18)

        # ── Advanced settings toggle ──
        self._adv_btn = Button(
            input_card, text="⚙  Advanced Settings  ▾",
            bg=CARD, fg=MUTED, font=(FONT_MONO, 8),
            relief=FLAT, bd=0, cursor="hand2",
            padx=10, pady=3,
            command=self._toggle_advanced)
        self._adv_btn.pack(anchor=W)

        self._adv_frame = Frame(input_card, bg=CARD)
        self._adv_open = False
        self._build_advanced(self._adv_frame)

        # ── Agent mode ──
        self._build_agent_card(parent)

    def _build_advanced(self, parent):
        params = [
            ("profit_min",           "Profit Min ($)",         "1.50"),
            ("loss_max",             "Loss Max ($)",           "5.0"),
            ("profit_trail_pct",     "Trail % Drop",           "15.0"),
            ("profit_trail_amount",  "Trail $ Drop",           "0.40"),
            ("loss_velocity",        "Loss Velocity ($)",      "3.0"),
            ("loss_velocity_window", "Velocity Window (s)",    "30"),
            ("max_spread",           "Max Spread (pts)",       "30"),
            ("warmup",               "Warmup (s)",             "3"),
            ("max_duration",         "Max Duration (s)",       "300"),
            ("breakeven_at",         "Breakeven At ($)",       "1.00"),
            ("breakeven_buffer",     "Breakeven Buffer ($)",   "0.20"),
            ("partial_close_pct",    "Partial Close (%)",      "0"),
            ("max_entry_slippage",   "Entry Slippage (pts)",   "30"),
            ("max_exit_slippage",    "Exit Slippage (pts)",    "100"),
            ("deviation",            "Deviation (pts)",        "30"),
            ("emergency_deviation",  "Emrg. Deviation (pts)",  "100"),
            ("magic",                "Magic Number",           "234567"),
        ]

        canvas = Canvas(parent, bg=CARD, highlightthickness=0, height=230)
        sb = ttk.Scrollbar(parent, orient=VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill=Y)
        canvas.pack(side=LEFT, fill=BOTH, expand=True)

        grid = Frame(canvas, bg=CARD)
        canvas.create_window((0, 0), window=grid, anchor=NW)
        grid.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Enter>",  lambda _: canvas.bind_all("<MouseWheel>",
            lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units")))
        canvas.bind("<Leave>",  lambda _: canvas.unbind_all("<MouseWheel>"))

        for row_i, (key, label, default) in enumerate(params):
            Label(grid, text=label, bg=CARD, fg=MUTED,
                  font=(FONT_MONO, 8), width=22, anchor=W
                  ).grid(row=row_i, column=0, padx=(8, 4), pady=2, sticky=W)
            v = StringVar(value=self.settings.get(key, default))
            self.vars[key] = v
            Entry(grid, textvariable=v, width=10,
                  bg=PANEL, fg=WHITE, insertbackground=WHITE,
                  font=(FONT_MONO, 9), relief=FLAT, bd=3
                  ).grid(row=row_i, column=1, padx=(0, 8), pady=2, sticky=W)

    def _toggle_advanced(self):
        if self._adv_open:
            self._adv_frame.pack_forget()
            self._adv_btn.config(text="⚙  Advanced Settings  ▾")
        else:
            self._adv_frame.pack(fill=X, pady=(0, 10))
            self._adv_btn.config(text="⚙  Advanced Settings  ▴")
        self._adv_open = not self._adv_open

    def _build_agent_card(self, parent):
        """Agent Mode: toggle, editable prompt, schedule + P&L guardrails."""
        outer = Frame(parent, bg=BORDER)
        outer.pack(fill=X, pady=(0, 6))
        card = Frame(outer, bg=CARD)
        card.pack(fill=BOTH, padx=1, pady=1)

        hdr = Frame(card, bg=CARD)
        hdr.pack(fill=X, padx=10, pady=(8, 4))
        Label(hdr, text="AGENT MODE", bg=CARD, fg=MUTED,
              font=(FONT_MONO, 8, "bold")).pack(side=LEFT)

        agent_cfg = agent.load_agent_config()

        self.agent_vars: dict = {
            "enabled":              BooleanVar(value=agent_cfg.enabled),
            "model":                StringVar(value=agent_cfg.model),
            "start_time":           StringVar(value=agent_cfg.start_time),
            "end_time":             StringVar(value=agent_cfg.end_time),
            "daily_loss_limit":     StringVar(value=str(agent_cfg.daily_loss_limit)),
            "daily_profit_target":  StringVar(value=str(agent_cfg.daily_profit_target)),
            "max_lots":             StringVar(value=str(agent_cfg.max_lots)),
            "decision_interval_s":  StringVar(value=str(agent_cfg.decision_interval_s)),
            "monitor_interval_s":   StringVar(value=str(agent_cfg.monitor_interval_s)),
            "allow_early_close":    BooleanVar(value=agent_cfg.allow_early_close),
        }

        self._agent_toggle_btn = Button(
            hdr, text="ON" if agent_cfg.enabled else "OFF",
            bg=BUY if agent_cfg.enabled else IND_OFF_BG,
            fg="#000" if agent_cfg.enabled else IND_OFF_FG,
            font=(FONT_MONO, 8, "bold"), relief=FLAT, bd=0,
            width=5, cursor="hand2", command=self._toggle_agent)
        self._agent_toggle_btn.pack(side=RIGHT)

        self._agent_status_lbl = Label(card, text="agent: off",
                                        bg=CARD, fg=MUTED, font=(FONT_MONO, 8))
        self._agent_status_lbl.pack(anchor=W, padx=10)

        Label(card, text="PROMPT", bg=CARD, fg=MUTED,
              font=(FONT_MONO, 7, "bold")).pack(anchor=W, padx=10, pady=(6, 0))
        self._agent_prompt_box = Text(
            card, height=5, bg=PANEL, fg=TEXT,
            insertbackground=WHITE, font=(FONT_MONO, 8),
            relief=FLAT, bd=4, wrap=WORD)
        self._agent_prompt_box.pack(fill=X, padx=10, pady=(2, 6))
        self._agent_prompt_box.insert("1.0", agent_cfg.prompt)

        grid = Frame(card, bg=CARD)
        grid.pack(fill=X, padx=10, pady=(0, 8))
        fields = [
            ("start_time",           "Start (HH:MM)"),
            ("end_time",             "End (HH:MM)"),
            ("daily_loss_limit",     "Daily Loss Limit ($)"),
            ("daily_profit_target",  "Daily Profit Target ($)"),
            ("max_lots",             "Max Lots"),
            ("decision_interval_s",  "Decision Every (s)"),
            ("monitor_interval_s",   "Monitor Every (s)"),
            ("model",                "Model (blank=default)"),
        ]
        for row_i, (key, label) in enumerate(fields):
            Label(grid, text=label, bg=CARD, fg=MUTED,
                  font=(FONT_MONO, 7), width=18, anchor=W
                  ).grid(row=row_i, column=0, pady=1, sticky=W)
            Entry(grid, textvariable=self.agent_vars[key], width=10,
                  bg=PANEL, fg=WHITE, insertbackground=WHITE,
                  font=(FONT_MONO, 8), relief=FLAT, bd=3
                  ).grid(row=row_i, column=1, pady=1, sticky=W)

        Checkbutton(
            card, text="Allow agent to request early close",
            variable=self.agent_vars["allow_early_close"],
            bg=CARD, fg=MUTED, selectcolor=PANEL,
            activebackground=CARD, activeforeground=MUTED,
            font=(FONT_MONO, 7), anchor=W,
            command=self._save_agent_settings,
        ).pack(fill=X, padx=8, pady=(0, 8))

    def _toggle_agent(self):
        enabled = not self.agent_vars["enabled"].get()
        self.agent_vars["enabled"].set(enabled)
        self._agent_toggle_btn.config(
            text="ON" if enabled else "OFF",
            bg=BUY if enabled else IND_OFF_BG,
            fg="#000" if enabled else IND_OFF_FG,
        )
        self._save_agent_settings()
        if enabled:
            self.agent_runner.start()
            self._log_agent("Agent mode enabled.")
        else:
            self.agent_runner.stop()
            self._log_agent("Agent mode disabled.")

    def _current_agent_config(self) -> "agent.AgentConfig":
        def _f(key, default):
            try:
                return float(self.agent_vars[key].get())
            except (ValueError, KeyError):
                return default

        def _i(key, default):
            try:
                return int(float(self.agent_vars[key].get()))
            except (ValueError, KeyError):
                return default

        prompt_text = self._agent_prompt_box.get("1.0", "end-1c").strip()
        return agent.AgentConfig(
            enabled=self.agent_vars["enabled"].get(),
            prompt=prompt_text or agent.DEFAULT_PROMPT,
            model=self.agent_vars["model"].get().strip(),
            start_time=self.agent_vars["start_time"].get().strip() or "00:00",
            end_time=self.agent_vars["end_time"].get().strip() or "23:59",
            daily_loss_limit=_f("daily_loss_limit", 10.0),
            daily_profit_target=_f("daily_profit_target", 20.0),
            max_lots=_f("max_lots", 0.05),
            decision_interval_s=_i("decision_interval_s", 60),
            monitor_interval_s=_i("monitor_interval_s", 30),
            allow_early_close=self.agent_vars["allow_early_close"].get(),
        )

    def _current_agent_symbol(self) -> str:
        return self.vars["symbol"].get().strip().upper() or "XAUUSD"

    def _current_agent_baseline(self) -> dict:
        return {k: v.get().strip() for k, v in self.vars.items()}

    def _refresh_agent_snapshots(self):
        """Refresh the plain-Python snapshots the agent's background thread
        reads, must only be called from the main thread (see __init__)."""
        self._agent_config_snapshot = self._current_agent_config()
        self._agent_symbol_snapshot = self._current_agent_symbol()
        self._agent_baseline_snapshot = self._current_agent_baseline()

    def _save_agent_settings(self):
        agent.save_agent_config(self._current_agent_config())

    def _agent_open_trade(self, trade_type: str, lots_str: str, overrides: dict):
        """Launch a trade on the agent's behalf, through the exact same code
        path as the manual BUY/SELL buttons -- the agent only supplies the
        lots/config values, everything downstream (parsing, indicators,
        history) is unchanged."""
        if self.bot_proc and self.bot_proc.poll() is None:
            self._log_agent("Agent requested a trade but one is already active — ignoring.")
            return

        prev_lots = self.vars["lots"].get()
        prev_overrides = {k: self.vars[k].get() for k in overrides if k in self.vars}
        self.vars["lots"].set(lots_str)
        for k, v in overrides.items():
            if k in self.vars:
                self.vars[k].set(str(v))

        self._log_agent(
            f"Opening {trade_type.upper()} {lots_str} lots"
            + (f" (overrides: {overrides})" if overrides else "")
        )
        try:
            self._open_trade(trade_type)
        finally:
            # Restore the user's baseline advanced settings so this one-off
            # trade's values never leak into the persisted ui_settings.json.
            self.vars["lots"].set(prev_lots)
            for k, v in prev_overrides.items():
                self.vars[k].set(v)
            _save(SETTINGS_FILE, {k: v.get().strip() for k, v in self.vars.items()})

    def _build_right(self, parent):
        # Row 0: Active trade
        self._build_active(parent)
        # Row 1: Stats bar + last trade
        self._build_stats_bar(parent)
        # Row 2: History (expands)
        self._build_history(parent)
        # Row 3: Log
        self._build_log(parent)
        # Row 4: Agent log (separate from bot log)
        self._build_agent_log(parent)

    def _build_active(self, parent):
        outer = Frame(parent, bg=BORDER)
        outer.grid(row=0, column=0, sticky=EW, pady=(0, 6))
        card = Frame(outer, bg=CARD)
        card.pack(fill=BOTH, padx=1, pady=1)

        # Header bar
        hbar = Frame(card, bg=CARD)
        hbar.pack(fill=X, padx=12, pady=(10, 6))
        Label(hbar, text="ACTIVE TRADE", bg=CARD, fg=MUTED,
              font=(FONT_MONO, 8, "bold")).pack(side=LEFT)
        self._duration_lbl = Label(hbar, text="",
                                    bg=CARD, fg=TEXT,
                                    font=(FONT_MONO, 12, "bold"))
        self._duration_lbl.pack(side=RIGHT)

        # Status line
        self._status_lbl = Label(card, text="No active trade",
                                  bg=CARD, fg=MUTED,
                                  font=(FONT_MONO, 14, "bold"))
        self._status_lbl.pack(anchor=W, padx=12)

        # P&L + metrics
        pnl_row = Frame(card, bg=CARD)
        pnl_row.pack(fill=X, padx=12, pady=(4, 6))

        self._pnl_lbl = Label(pnl_row, text="—",
                               bg=CARD, fg=MUTED,
                               font=(FONT_MONO, 42, "bold"), width=9, anchor=W)
        self._pnl_lbl.pack(side=LEFT)

        metrics = Frame(pnl_row, bg=CARD)
        metrics.pack(side=LEFT, padx=8, anchor=S)
        self._peak_lbl    = self._stat_box(metrics, "PEAK",     "—")
        self._maxloss_lbl = self._stat_box(metrics, "MAX LOSS", "—")

        # State indicators
        ind_row = Frame(card, bg=CARD)
        ind_row.pack(fill=X, padx=12, pady=(0, 6))

        self._indicators: dict[str, tuple] = {}
        for name, act_bg, act_fg in [
            ("WARMUP",    ACCENT,    "#000"),
            ("BREAKEVEN", "#3377ee", WHITE),
            ("TRAILING",  BUY,       "#000"),
            ("PARTIAL",   "#9944cc", WHITE),
        ]:
            f = Frame(ind_row, bg=IND_OFF_BG)
            f.pack(side=LEFT, padx=2)
            lbl = Label(f, text=name, bg=IND_OFF_BG, fg=IND_OFF_FG,
                        font=(FONT_MONO, 8, "bold"), padx=8, pady=4)
            lbl.pack()
            self._indicators[name] = (f, lbl, act_bg, act_fg)

        # Info row (ticket / entry / ticks)
        info_row = Frame(card, bg=CARD)
        info_row.pack(fill=X, padx=12, pady=(0, 6))
        self._ticket_lbl = Label(info_row, text="Ticket: —",
                                  bg=CARD, fg=MUTED, font=(FONT_MONO, 9))
        self._ticket_lbl.pack(side=LEFT)
        self._entry_lbl = Label(info_row, text="",
                                 bg=CARD, fg=MUTED, font=(FONT_MONO, 9))
        self._entry_lbl.pack(side=LEFT, padx=14)
        self._ticks_lbl = Label(info_row, text="",
                                 bg=CARD, fg=MUTED, font=(FONT_MONO, 9))
        self._ticks_lbl.pack(side=RIGHT)

        # CLOSE button
        self._close_btn = Button(
            card, text="✕  CLOSE TRADE",
            bg=IND_OFF_BG, fg=IND_OFF_FG,
            font=(FONT_MONO, 13, "bold"), relief=FLAT, bd=0,
            state=DISABLED, cursor="arrow",
            command=self._close_trade)
        self._close_btn.pack(fill=X, padx=12, pady=(0, 12), ipady=12)

    def _stat_box(self, parent: Frame, label: str, val: str) -> Label:
        f = Frame(parent, bg=CARD)
        f.pack(side=LEFT, padx=10)
        Label(f, text=label, bg=CARD, fg=MUTED,
              font=(FONT_MONO, 7, "bold")).pack()
        v = Label(f, text=val, bg=CARD, fg=MUTED, font=(FONT_MONO, 12))
        v.pack()
        return v

    def _build_stats_bar(self, parent):
        outer = Frame(parent, bg=BORDER)
        outer.grid(row=1, column=0, sticky=EW, pady=(0, 6))
        bar = Frame(outer, bg=PANEL)
        bar.pack(fill=BOTH, padx=1, pady=1)

        inner = Frame(bar, bg=PANEL)
        inner.pack(fill=X, padx=10, pady=7)

        # Last trade (left)
        self._last_lbl = Label(inner, text="Last trade: —",
                                bg=PANEL, fg=MUTED,
                                font=(FONT_MONO, 9), anchor=W)
        self._last_lbl.pack(side=LEFT)

        # Today stats (right)
        stats_f = Frame(inner, bg=PANEL)
        stats_f.pack(side=RIGHT)

        self._slbls: dict[str, Label] = {}
        for key, txt in [("sep", "│"), ("ttrades", "Today: 0 trades"),
                          ("twins", "W:0"), ("tlosses", "L:0"),
                          ("tpnl", "$0.00"), ("sep2", "│"), ("apnl", "All-time: $0.00")]:
            if key.startswith("sep"):
                Label(stats_f, text=txt, bg=PANEL, fg=BORDER,
                      font=(FONT_MONO, 10)).pack(side=LEFT, padx=4)
            else:
                lbl = Label(stats_f, text=txt, bg=PANEL, fg=MUTED,
                            font=(FONT_MONO, 9))
                lbl.pack(side=LEFT, padx=5)
                self._slbls[key] = lbl

    def _build_history(self, parent):
        outer = Frame(parent, bg=BORDER)
        outer.grid(row=2, column=0, sticky=NSEW, pady=(0, 6))
        card = Frame(outer, bg=CARD)
        card.pack(fill=BOTH, expand=True, padx=1, pady=1)

        Label(card, text="TRADE HISTORY", bg=CARD, fg=MUTED,
              font=(FONT_MONO, 8, "bold"), padx=8, pady=3, anchor=W
              ).pack(fill=X)

        # Clear button
        def _clear():
            if messagebox.askyesno("Clear History",
                                   "Delete all trade history? This cannot be undone."):
                self.history.clear()
                _save(HISTORY_FILE, self.history)
                self._refresh_table()
                self._refresh_stats()
        Button(card, text="clear", bg=CARD, fg=MUTED,
               font=(FONT_MONO, 7), relief=FLAT, bd=0, cursor="hand2",
               command=_clear).place(relx=1.0, y=0, anchor=NE, x=-8)

        cols   = ("time", "sym", "type", "lots", "entry", "exit",
                  "pnl", "comm", "peak", "reason", "dur")
        hdrs   = ("Time", "Symbol", "Type", "Lots", "Entry $", "Exit $",
                  "Net P&L", "Commis.", "Peak", "Reason", "Dur(s)")
        widths = (72, 72, 44, 52, 78, 78, 76, 66, 76, 118, 52)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Hist.Treeview",
                        background=PANEL, foreground=TEXT,
                        fieldbackground=PANEL, borderwidth=0,
                        rowheight=21, font=(FONT_MONO, 9))
        style.configure("Hist.Treeview.Heading",
                        background=CARD, foreground=MUTED,
                        relief="flat", font=(FONT_MONO, 8, "bold"),
                        borderwidth=0)
        style.map("Hist.Treeview",
                  background=[("selected", BORDER)],
                  foreground=[("selected", WHITE)])

        tree_f = Frame(card, bg=CARD)
        tree_f.pack(fill=BOTH, expand=True)

        self._tree = ttk.Treeview(tree_f, columns=cols, show="headings",
                                   style="Hist.Treeview", height=8)
        for col, hdr, w in zip(cols, hdrs, widths):
            anchor = W if col in ("reason",) else CENTER
            self._tree.heading(col, text=hdr,
                               command=lambda c=col: self._sort_col(c))
            self._tree.column(col, width=w, anchor=anchor,
                              stretch=(col == "reason"))

        vsb = ttk.Scrollbar(tree_f, orient=VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side=LEFT, fill=BOTH, expand=True)
        vsb.pack(side=RIGHT, fill=Y)

        self._tree.tag_configure("win",  foreground=PROFIT)
        self._tree.tag_configure("loss", foreground=LOSS)
        self._tree.tag_configure("flat", foreground=MUTED)

        self._sort_reverse: dict[str, bool] = {}

    def _sort_col(self, col: str):
        rev = self._sort_reverse.get(col, False)
        items = [(self._tree.set(k, col), k) for k in self._tree.get_children()]
        try:
            items.sort(key=lambda x: float(
                x[0].replace("$", "").replace("+", "") or "0"), reverse=rev)
        except ValueError:
            items.sort(reverse=rev)
        for i, (_, k) in enumerate(items):
            self._tree.move(k, "", i)
        self._sort_reverse[col] = not rev

    def _build_log(self, parent):
        outer = Frame(parent, bg=BORDER)
        outer.grid(row=3, column=0, sticky=EW)
        card = Frame(outer, bg=CARD)
        card.pack(fill=BOTH, padx=1, pady=1)

        Label(card, text="BOT LOG", bg=CARD, fg=MUTED,
              font=(FONT_MONO, 8, "bold"), padx=8, pady=3, anchor=W
              ).pack(fill=X)

        self._log_box = Text(
            card, height=6, bg=PANEL, fg=MUTED,
            font=(FONT_MONO, 8), relief=FLAT, state=DISABLED,
            wrap=WORD, selectbackground=BORDER, cursor="arrow")
        self._log_box.pack(fill=X, padx=1, pady=(0, 1))

        for tag, fg in [
            ("INFO",     "#7788cc"),
            ("WARNING",  ACCENT),
            ("ERROR",    LOSS),
            ("CRITICAL", LOSS),
            ("DEBUG",    "#383848"),
            ("UI",       MUTED),
        ]:
            self._log_box.tag_config(tag, foreground=fg)

    def _build_agent_log(self, parent):
        """Separate log panel for the agent's decisions/reasoning -- kept
        mechanically distinct from BOT LOG (which is main.py's execution
        log)."""
        outer = Frame(parent, bg=BORDER)
        outer.grid(row=4, column=0, sticky=EW, pady=(6, 0))
        card = Frame(outer, bg=CARD)
        card.pack(fill=BOTH, padx=1, pady=1)

        Label(card, text="AGENT LOG", bg=CARD, fg=MUTED,
              font=(FONT_MONO, 8, "bold"), padx=8, pady=3, anchor=W
              ).pack(fill=X)

        self._agent_log_box = Text(
            card, height=6, bg=PANEL, fg=MUTED,
            font=(FONT_MONO, 8), relief=FLAT, state=DISABLED,
            wrap=WORD, selectbackground=BORDER, cursor="arrow")
        self._agent_log_box.pack(fill=X, padx=1, pady=(0, 1))

        for tag, fg in [
            ("open",  BUY),
            ("skip",  MUTED),
            ("close", ACCENT),
            ("error", LOSS),
        ]:
            self._agent_log_box.tag_config(tag, foreground=fg)

    # ─── Trade actions ─────────────────────────────────────────────────────────

    def _open_trade(self, trade_type: str):
        if self.bot_proc and self.bot_proc.poll() is None:
            messagebox.showerror("Bot Active",
                                 "A trade is already running.\nClose it first.")
            return

        settings = {k: v.get().strip() for k, v in self.vars.items()}
        _save(SETTINGS_FILE, settings)

        symbol = settings.get("symbol", "").upper()
        lots   = settings.get("lots", "")

        if not symbol:
            messagebox.showerror("Error", "Symbol cannot be empty.")
            return
        try:
            float(lots)
        except ValueError:
            messagebox.showerror("Error", f"Invalid lots: {lots!r}")
            return

        main_py = Path(__file__).parent / "main.py"
        cmd = [sys.executable, str(main_py), lots,
               "-symbol", symbol, "-type", trade_type]
        for key, arg in ARG_MAP.items():
            val = settings.get(key, DEFAULTS.get(key, ""))
            if val:
                cmd += [arg, val]

        try:
            self.bot_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True, bufsize=1,
                cwd=str(Path(__file__).parent),
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except Exception as e:
            messagebox.showerror("Launch Error", str(e))
            return

        now = time.time()
        self.active = {
            "symbol":    symbol,
            "type":      trade_type,
            "lots":      lots,
            "ticket":    None,
            "entry":     None,
            "exit":      None,
            "peak":      None,
            "open_ts":   now,
            "open_time": datetime.now().strftime("%H:%M:%S"),
            "date":      date.today().isoformat(),
        }
        self.live_pnl      = 0.0
        self.live_peak     = 0.0
        self.live_maxloss  = 0.0
        self.log_file_path = None
        self.log_file_pos  = 0
        self._in_summary   = False
        self._summary_buf  = {}

        color = BUY if trade_type == "buy" else SELL
        arrow = "▲ BUY" if trade_type == "buy" else "▼ SELL"
        self._status_lbl.config(
            text=f"{arrow}   {lots} lots   {symbol}",
            fg=color, font=(FONT_MONO, 14, "bold"))
        self._pnl_lbl.config(text="$0.00", fg=MUTED)
        self._peak_lbl.config(text="—",  fg=MUTED)
        self._maxloss_lbl.config(text="—", fg=MUTED)
        self._ticket_lbl.config(text="Ticket: filling…")
        self._entry_lbl.config(text="")
        self._ticks_lbl.config(text="")
        self._set_ind("WARMUP", True)

        self.buy_btn.config(state=DISABLED, bg=IND_OFF_BG, fg="#555")
        self.sell_btn.config(state=DISABLED, bg=IND_OFF_BG, fg="#555")
        self._close_btn.config(state=NORMAL, bg=SELL, fg=WHITE,
                                cursor="hand2", text="✕  CLOSE TRADE")
        self._conn_lbl.config(text="● LIVE", fg=PROFIT)

        self._log_ui(f"Launched {trade_type.upper()} {lots} {symbol}")
        threading.Thread(target=self._stream_stdout, daemon=True).start()

    def _close_trade(self):
        """Tell the bot to close its MT5 position by writing a stop-file.

        The trade loop checks for this file every ~50 ms iteration.  When it
        finds the file it sets exit_reason='manual_shutdown', deletes the file,
        and exits, triggering the normal close-position-with-retry path.

        This approach works regardless of whether there is an attached console
        (console control events are unreliable from a GUI process on Windows).
        """
        if self.bot_proc and self.bot_proc.poll() is None:
            magic = self.vars.get("magic")
            magic_val = magic.get() if magic else DEFAULTS["magic"]
            stop_file = Path(__file__).parent / f".close_{magic_val}"
            try:
                stop_file.write_text("close")
                self._log_ui(f"Stop-file written — bot will close MT5 position…")
            except Exception as e:
                self._log_ui(f"Could not write stop-file ({e}), terminating process.")
                self.bot_proc.terminate()
        else:
            self._finalize("manual_abort", None, None, None)

    # ─── Background I/O ────────────────────────────────────────────────────────

    def _stream_stdout(self):
        proc = self.bot_proc
        try:
            for line in proc.stdout:
                self.log_q.put(("LOG", line.rstrip()))
                # Auto-respond to "existing position" prompts
                if "manage existing" in line.lower() or "open new" in line.lower():
                    try:
                        proc.stdin.write("new\n")
                        proc.stdin.flush()
                    except Exception:
                        pass
        except Exception as e:
            self.log_q.put(("LOG", f"[stream error] {e}"))
        self.log_q.put(("DONE", None))

    def _tick(self):
        """Main-thread 200 ms heartbeat: drain queue + update duration."""
        self._drain_q()
        self._refresh_agent_snapshots()
        if self.active:
            self._poll_logfile()
            elapsed = int(time.time() - self.active["open_ts"])
            m, s = divmod(elapsed, 60)
            self._duration_lbl.config(text=f"{m:02d}:{s:02d}")
        else:
            self._duration_lbl.config(text="")
        self._agent_status_lbl.config(text=f"agent: {self.agent_runner.state}")
        self.root.after(200, self._tick)

    def _drain_q(self):
        try:
            while True:
                kind, data = self.log_q.get_nowait()
                if kind == "DONE":
                    self._on_bot_done()
                elif kind == "LOG":
                    self._handle_line(data)
                elif kind == "PNL":
                    self._apply_pnl(float(data))
                elif kind == "AGENT_LOG":
                    self._append_agent_log(data)
                elif kind == "AGENT_OPEN":
                    trade_type, lots_str, overrides = data
                    self._agent_open_trade(trade_type, lots_str, overrides)
                elif kind == "AGENT_CLOSE":
                    self._close_trade()
        except queue.Empty:
            pass

    def _poll_logfile(self):
        """Tail the debug log file (written by the bot) for tick-level P&L."""
        if self.log_file_path is None:
            logs_dir = Path(__file__).parent / "logs"
            if logs_dir.exists():
                candidates = sorted(
                    logs_dir.glob("*.log"),
                    key=lambda f: f.stat().st_mtime,
                    reverse=True)
                if candidates:
                    newest = candidates[0]
                    if newest.stat().st_mtime >= (self.active["open_ts"] - 5):
                        self.log_file_path = newest
                        self.log_file_pos  = 0
                        self._log_ui(f"Tailing {newest.name}")

        if self.log_file_path and self.log_file_path.exists():
            try:
                with open(self.log_file_path, encoding="utf-8", errors="ignore") as fh:
                    fh.seek(self.log_file_pos)
                    for raw in fh:
                        raw = raw.rstrip()
                        # Debug lines: "... [DEBUG] P&L: $-1.87 | spread: ..."
                        # The bot logs exactly: f"P&L: ${current_pnl:.2f} | ..."
                        m = re.search(r"\[DEBUG\]\s+P&L:\s+\$?([-\d.]+)", raw)
                        if m:
                            try:
                                self.log_q.put(("PNL", float(m.group(1))))
                            except ValueError:
                                pass
                    self.log_file_pos = fh.tell()
            except Exception:
                pass

    # ─── Log parsing ───────────────────────────────────────────────────────────

    def _handle_line(self, line: str):
        self._append_log(line)
        if not line:
            return

        ll = line.lower()

        # ── Always parse account info (appears before trade_state.active is set) ──
        m = re.search(r"account:\s*#(\d+).*balance:\s*([\d.]+)\s*(\w+)", line, re.I)
        if m:
            self._acct_lbl.config(
                text=f"#{m.group(1)}  {m.group(2)} {m.group(3)}",
                fg=TEXT)

        # ── Trade summary block ───────────────────────────────────────────────────
        # The bot logs a single logger.info() call whose message contains newlines.
        # Each newline in that message arrives as a separate stdout line.
        # First line: "... [INFO] === TRADE SUMMARY ==="  (has the [INFO] prefix)
        # Subsequent lines: "  reason: profit_trailing"   (no prefix)
        #
        # We buffer the key-value lines and consume the buffer in _on_bot_done,
        # which is called after the stdout pipe closes.  By then every summary
        # line is already in the queue ahead of the DONE sentinel.

        if "=== TRADE SUMMARY ===" in line:
            self._in_summary = True
            self._summary_buf = {}
            return

        if self._in_summary:
            # Each summary line: "  key: value"
            # exact bot format strings:
            #   f"  reason: {trade_state.exit_reason}\n"
            #   f"  final_pnl: ${trade_state.current_pnl:.2f}\n"   e.g. $-1.87
            #   f"  peak_profit: ${trade_state.peak_profit:.2f}\n"
            #   f"  exit_price: {exit_price}\n"
            #   f"  total_ticks: {trade_state.total_ticks}\n"
            stripped = line.strip()

            m = re.match(r"reason:\s*(\S+)", stripped)
            if m:
                self._summary_buf["reason"] = m.group(1)

            m = re.match(r"final_pnl:\s*\$?([-\d.]+)", stripped)
            if m:
                self._summary_buf["final_pnl"] = float(m.group(1))

            m = re.match(r"gross_pnl:\s*\$?([-\d.]+)", stripped)
            if m:
                self._summary_buf["gross_pnl"] = float(m.group(1))

            m = re.match(r"commission:\s*\$?([-\d.]+)", stripped)
            if m:
                self._summary_buf["commission"] = float(m.group(1))

            m = re.match(r"swap:\s*\$?([-\d.]+)", stripped)
            if m:
                self._summary_buf["swap"] = float(m.group(1))

            m = re.match(r"peak_profit:\s*\$?([-\d.]+)", stripped)
            if m:
                self._summary_buf["peak"] = float(m.group(1))

            m = re.match(r"exit_price:\s*([\d.]+)", stripped)
            if m:
                self._summary_buf["exit_price"] = m.group(1)

            m = re.match(r"total_ticks:\s*(\d+)", stripped)
            if m:
                self._ticks_lbl.config(
                    text=f"Ticks: {m.group(1)}", fg=MUTED)
            return  # don't process summary lines further

        # ── Active-trade-only parsing below ──────────────────────────────────────
        if not self.active:
            return

        # Ticket number
        if self.active.get("ticket") is None:
            m = re.search(r"ticket[=:\s#]+(\d+)", line, re.I)
            if m:
                self.active["ticket"] = m.group(1)
                self._ticket_lbl.config(text=f"Ticket: #{m.group(1)}")

        # Entry price — bot logs: "price={trade_state.open_price}, lots=..."
        if self.active.get("entry") is None:
            m = (re.search(r"price[=:\s]+([\d.]+),\s*lots", line, re.I)
                 or re.search(r"open(?:ed)?\s+(?:price|at)[=:\s]+([\d.]+)", line, re.I))
            if m:
                self.active["entry"] = m.group(1)
                self._entry_lbl.config(text=f"@ {m.group(1)}", fg=TEXT)

        # Warmup ended
        # bot logs: "Warmup period ended at X.Xs, all exit conditions now active"
        if "warmup period ended" in ll:
            self._set_ind("WARMUP", False)

        # Breakeven activated
        if "breakeven" in ll and "activated" in ll:
            self._set_ind("BREAKEVEN", True)

        # Profit trailing activated
        # bot logs: "Profit target activated at $X.XX"
        if "profit target activated" in ll:
            self._set_ind("TRAILING", True)

        # Partial close
        if "partial close" in ll:
            self._set_ind("PARTIAL", True)

        # Peak profit milestone — bot logs: "New peak profit milestone: $X.XX"
        # The value here is the peak, NOT the current P&L — update peak display only.
        m = re.search(r"peak profit milestone:\s*\$?([\d.]+)", line, re.I)
        if m:
            try:
                pk = float(m.group(1))
                if pk > self.live_peak:
                    self.live_peak = pk
                    if self.active:
                        self.active["peak"] = pk
                    self._peak_lbl.config(text=f"+${pk:.2f}", fg=PROFIT)
            except ValueError:
                pass

        # Reconnect events
        if "reconnected after" in ll:
            self._conn_lbl.config(text="● LIVE", fg=PROFIT)
        elif "reconnect" in ll or "disconnect" in ll:
            self._conn_lbl.config(text="● RECONNECTING…", fg=ACCENT)

    def _apply_pnl(self, pnl: float):
        self.live_pnl = pnl
        color = PROFIT if pnl >= 0 else LOSS
        sign  = "+" if pnl >= 0 else ""
        self._pnl_lbl.config(text=f"{sign}${pnl:.2f}", fg=color)

        if pnl > self.live_peak:
            self.live_peak = pnl
            if self.active:
                self.active["peak"] = pnl
            self._peak_lbl.config(text=f"+${pnl:.2f}", fg=PROFIT)

        cur_loss = abs(min(0.0, pnl))
        if cur_loss > self.live_maxloss:
            self.live_maxloss = cur_loss
            self._maxloss_lbl.config(text=f"-${cur_loss:.2f}", fg=LOSS)

    def _on_bot_done(self):
        """Bot process stdout closed.  Use the summary buffer if populated,
        otherwise fall back to the last live values observed during the trade."""
        self._in_summary = False  # leave summary mode regardless

        if not self.active:
            return

        if self._summary_buf:
            # The trade summary was fully received — use its authoritative values.
            pnl        = self._summary_buf.get("final_pnl")   # correct sign
            peak       = self._summary_buf.get("peak", self.live_peak or None)
            reason     = self._summary_buf.get("reason", "bot_exited")
            exit_price = self._summary_buf.get("exit_price")
            commission = self._summary_buf.get("commission")
            swap       = self._summary_buf.get("swap")
        else:
            # Summary never arrived (crash / very early exit).
            pnl        = self.live_pnl  if self.live_pnl  != 0.0 else None
            peak       = self.live_peak if self.live_peak != 0.0 else None
            reason     = "bot_exited"
            exit_price = None
            commission = None
            swap       = None

        self._finalize(reason, pnl, peak, exit_price, commission, swap)

    def _finalize(self, reason: str, pnl, peak, exit_price,
                  commission=None, swap=None):
        if not self.active:
            return

        trade = dict(self.active)
        trade.update({
            "close_time":  datetime.now().strftime("%H:%M:%S"),
            "close_ts":    time.time(),
            "exit_reason": reason,
            "pnl":         pnl,
            "peak":        peak,
            "exit":        exit_price,
            "commission":  commission,
            "swap":        swap,
            "duration":    int(time.time() - trade.get("open_ts", time.time())),
        })
        self.history.append(trade)
        _save(HISTORY_FILE, self.history)

        # Last trade label
        def _fmt(v):
            if v is None:
                return "—"
            return f"{'+'if v>=0 else ''}${v:.2f}"

        pnl_color = PROFIT if (pnl and pnl > 0) else (LOSS if (pnl and pnl < 0) else MUTED)
        ttype = trade["type"].upper()
        self._last_lbl.config(
            text=(f"Last: {ttype} {trade['lots']} {trade['symbol']}"
                  f"   [{reason}]   P&L {_fmt(pnl)}"
                  f"   Peak {_fmt(peak)}   {trade['duration']}s"),
            fg=pnl_color)

        self._refresh_table()
        self._refresh_stats()

        # Reset state
        self.active        = None
        self.live_pnl      = 0.0
        self.live_peak     = 0.0
        self.live_maxloss  = 0.0
        self.log_file_path = None

        # Reset widgets
        self._status_lbl.config(text="No active trade",
                                 fg=MUTED, font=(FONT_MONO, 14, "bold"))
        self._pnl_lbl.config(text="—", fg=MUTED)
        self._peak_lbl.config(text="—", fg=MUTED)
        self._maxloss_lbl.config(text="—", fg=MUTED)
        self._ticket_lbl.config(text="Ticket: —")
        self._entry_lbl.config(text="")
        self._ticks_lbl.config(text="")
        for name in self._indicators:
            self._set_ind(name, False)
        self.buy_btn.config(state=NORMAL, bg=BUY,       fg="#000")
        self.sell_btn.config(state=NORMAL, bg=SELL,     fg=WHITE)
        self._close_btn.config(state=DISABLED, bg=IND_OFF_BG,
                                fg=IND_OFF_FG, cursor="arrow")
        self._conn_lbl.config(text="● OFFLINE", fg=MUTED)

    # ─── Indicators ────────────────────────────────────────────────────────────

    def _set_ind(self, name: str, on: bool):
        frame, lbl, act_bg, act_fg = self._indicators[name]
        if on:
            frame.config(bg=act_bg)
            lbl.config(bg=act_bg, fg=act_fg)
        else:
            frame.config(bg=IND_OFF_BG)
            lbl.config(bg=IND_OFF_BG, fg=IND_OFF_FG)

    # ─── History table ─────────────────────────────────────────────────────────

    def _refresh_table(self):
        for row in self._tree.get_children():
            self._tree.delete(row)

        def _fmt(v):
            if v is None:
                return "—"
            return f"{'+'if v>=0 else ''}${v:.2f}"

        for t in reversed(self.history[-300:]):
            pnl  = t.get("pnl")
            comm = t.get("commission")
            peak = t.get("peak")
            tag  = ("win"  if (pnl is not None and pnl > 0) else
                    "loss" if (pnl is not None and pnl < 0) else "flat")
            self._tree.insert("", END, values=(
                t.get("open_time",  ""),
                t.get("symbol",     ""),
                t.get("type",       "").upper(),
                t.get("lots",       ""),
                t.get("entry",      "—") or "—",
                t.get("exit",       "—") or "—",
                _fmt(pnl),
                _fmt(comm),
                _fmt(peak),
                t.get("exit_reason", ""),
                t.get("duration",   ""),
            ), tags=(tag,))

    # ─── Stats bar ─────────────────────────────────────────────────────────────

    def _refresh_stats(self):
        today    = date.today().isoformat()
        td       = [t for t in self.history if t.get("date") == today]
        td_pnls  = [t["pnl"] for t in td if t.get("pnl") is not None]
        all_pnls = [t["pnl"] for t in self.history if t.get("pnl") is not None]

        wins   = sum(1 for p in td_pnls if p > 0)
        losses = sum(1 for p in td_pnls if p <= 0)
        tp     = sum(td_pnls)
        ap     = sum(all_pnls)

        def _c(v): return PROFIT if v >= 0 else LOSS
        def _f(v): return f"{'+'if v>=0 else ''}${v:.2f}"

        self._slbls["ttrades"].config(
            text=f"Today: {len(td)} trade{'s'if len(td)!=1 else ''}",
            fg=MUTED)
        self._slbls["twins"].config(  text=f"W:{wins}",   fg=PROFIT)
        self._slbls["tlosses"].config(text=f"L:{losses}",  fg=LOSS)
        self._slbls["tpnl"].config(   text=_f(tp),         fg=_c(tp))
        self._slbls["apnl"].config(   text=f"All-time: {_f(ap)}", fg=_c(ap))

    # ─── Log view ──────────────────────────────────────────────────────────────

    def _append_log(self, line: str):
        tag = "INFO"
        for lv in ("CRITICAL", "ERROR", "WARNING", "DEBUG"):
            if f"[{lv}]" in line:
                tag = lv
                break

        self._log_box.config(state=NORMAL)
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_box.insert(END, f"{ts}  {line}\n", tag)
        self._log_box.see(END)
        n = int(self._log_box.index("end-1c").split(".")[0])
        if n > 1200:
            self._log_box.delete("1.0", f"{n - 1200}.0")
        self._log_box.config(state=DISABLED)

    def _log_ui(self, msg: str):
        self._append_log(f"[UI] {msg}")

    def _append_agent_log(self, msg: str):
        tag = "skip"
        low = msg.lower()
        if "error" in low or "timed out" in low or "not found" in low:
            tag = "error"
        elif "open" in low:
            tag = "open"
        elif "close" in low:
            tag = "close"

        self._agent_log_box.config(state=NORMAL)
        ts = datetime.now().strftime("%H:%M:%S")
        self._agent_log_box.insert(END, f"{ts}  {msg}\n", tag)
        self._agent_log_box.see(END)
        n = int(self._agent_log_box.index("end-1c").split(".")[0])
        if n > 1200:
            self._agent_log_box.delete("1.0", f"{n - 1200}.0")
        self._agent_log_box.config(state=DISABLED)

    def _log_agent(self, msg: str):
        self._append_agent_log(msg)

    # ─── shutdown ───────────────────────────────────────────────────────────

    def _on_close(self):
        self._save_agent_settings()
        self.agent_runner.stop()
        self.root.destroy()


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    root = Tk()
    dash = Dashboard(root)
    root.protocol("WM_DELETE_WINDOW", dash._on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
