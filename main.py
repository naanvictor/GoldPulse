#!/usr/bin/env python
"""GoldPulse — MT5 high-speed scalping bot for volatile instruments like XAUUSD."""

import argparse
import asyncio
import collections
import logging
import math
import os
import signal
import sys
import time
from collections import namedtuple
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import MetaTrader5 as mt5

TickEntry = namedtuple("TickEntry", ["timestamp", "bid", "ask"])


@dataclass
class TradeConfig:
    """Configuration for a single trade, populated from CLI arguments."""

    symbol: str
    trade_type: str  # "buy" or "sell"
    lots: float
    profit_min: float = 1.50
    loss_max: float = 5.0
    profit_trail_pct: float = 15.0
    profit_trail_amount: float = 0.40
    loss_velocity: float = 3.0
    loss_velocity_window: int = 30
    max_spread: int = 30
    warmup: int = 3
    max_duration: int = 300
    breakeven_at: float = 1.00
    breakeven_buffer: float = 0.20
    partial_close_pct: float = 0.0
    max_entry_slippage: int = 30
    max_exit_slippage: int = 100
    deviation: int = 30
    emergency_deviation: int = 100
    magic: int = 234567


@dataclass
class TradeState:
    """Mutable state tracked during the lifetime of a trade."""

    ticket: int = 0
    open_price: float = 0.0
    open_time: float = 0.0
    peak_profit: float = 0.0
    max_loss: float = 0.0
    profit_activated: bool = False
    breakeven_activated: bool = False
    partial_closed: bool = False
    remaining_lots: float = 0.0
    exit_reason: str = ""
    current_pnl: float = 0.0
    total_ticks: int = 0
    time_in_profit: float = 0.0
    time_in_loss: float = 0.0
    last_pnl_timestamp: float = 0.0
    max_profit_velocity: float = 0.0  # fastest $/sec gain observed
    max_loss_velocity: float = 0.0  # fastest $/sec loss observed
    total_disconnect_seconds: float = 0.0  # accumulated disconnect time
    entry_slippage_points: float = 0.0
    exit_slippage_points: float = 0.0
    profit_activation_time: float = 0.0
    partial_close_pnl: float = 0.0
    entry_commission: float = 0.0   # commission on the entry deal (negative)
    total_commission: float = 0.0   # sum of all deal commissions (negative)
    total_swap: float = 0.0         # sum of all deal swaps


def round_to_lot_step(lots: float, volume_min: float, volume_step: float) -> float:
    """Round a lot size down to the nearest valid lot step."""
    steps = math.floor((lots - volume_min) / volume_step)
    return volume_min + steps * volume_step


def setup_logging(symbol: str, trade_type: str) -> logging.Logger:
    """Configure logging with console (INFO) and file (DEBUG) handlers."""
    logger = logging.getLogger("scalper")
    logger.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(console)

    os.makedirs("logs", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"logs/{symbol}_{trade_type}_{timestamp}.log"
    file_handler = logging.FileHandler(filename)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(file_handler)

    return logger


def parse_args() -> TradeConfig:
    """Parse CLI arguments into a TradeConfig."""
    parser = argparse.ArgumentParser(description="GoldPulse — MT5 high-speed scalping bot")
    parser.add_argument("lots", type=float, help="Position size in lots")
    parser.add_argument("-symbol", required=True, type=str)
    parser.add_argument(
        "-type", required=True, choices=["buy", "sell"], dest="trade_type"
    )
    parser.add_argument("-profit-min", type=float, default=1.50)
    parser.add_argument("-loss-max", type=float, default=5.0)
    parser.add_argument("-profit-trail-pct", type=float, default=15.0)
    parser.add_argument("-profit-trail-amount", type=float, default=0.40)
    parser.add_argument("-loss-velocity", type=float, default=3.0)
    parser.add_argument("-loss-velocity-window", type=int, default=30)
    parser.add_argument("-max-spread", type=int, default=30)
    parser.add_argument("-warmup", type=int, default=3)
    parser.add_argument("-max-duration", type=int, default=300)
    parser.add_argument("-breakeven-at", type=float, default=1.00)
    parser.add_argument("-breakeven-buffer", type=float, default=0.20)
    parser.add_argument("-partial-close-pct", type=float, default=0)
    parser.add_argument("-max-entry-slippage", type=int, default=30)
    parser.add_argument("-max-exit-slippage", type=int, default=100)
    parser.add_argument("-deviation", type=int, default=30)
    parser.add_argument("-emergency-deviation", type=int, default=100)
    parser.add_argument("-magic", type=int, default=234567)

    args = parser.parse_args()

    return TradeConfig(
        symbol=args.symbol,
        trade_type=args.trade_type,
        lots=args.lots,
        profit_min=args.profit_min,
        loss_max=args.loss_max,
        profit_trail_pct=args.profit_trail_pct,
        profit_trail_amount=args.profit_trail_amount,
        loss_velocity=args.loss_velocity,
        loss_velocity_window=args.loss_velocity_window,
        max_spread=args.max_spread,
        warmup=args.warmup,
        max_duration=args.max_duration,
        breakeven_at=args.breakeven_at,
        breakeven_buffer=args.breakeven_buffer,
        partial_close_pct=args.partial_close_pct,
        max_entry_slippage=args.max_entry_slippage,
        max_exit_slippage=args.max_exit_slippage,
        deviation=args.deviation,
        emergency_deviation=args.emergency_deviation,
        magic=args.magic,
    )


async def validate(config: TradeConfig, logger: logging.Logger):
    """Run all pre-trade validation checks. Returns symbol_info on success."""
    # 1. Initialize MT5
    logger.debug("Initializing MT5 terminal...")
    if not await asyncio.to_thread(mt5.initialize):
        print("MT5 terminal not found or not running")
        sys.exit(1)

    terminal_info = await asyncio.to_thread(mt5.terminal_info)
    if terminal_info is not None:
        logger.debug(
            f"MT5 terminal: build={terminal_info.build}, "
            f"trade_allowed={terminal_info.trade_allowed}, "
            f"connected={terminal_info.connected}"
        )
        if not terminal_info.trade_allowed:
            print(
                "AutoTrading is disabled in MT5 terminal. "
                "Enable it via Tools > Options > Expert Advisors, "
                "or click the AutoTrading button in the toolbar."
            )
            sys.exit(1)

    account_info = await asyncio.to_thread(mt5.account_info)
    if account_info is not None:
        logger.info(
            f"Account: #{account_info.login} | "
            f"Balance: {account_info.balance} {account_info.currency} | "
            f"Leverage: 1:{account_info.leverage} | "
            f"Server: {account_info.server}"
        )
    else:
        logger.warning("Could not retrieve account info")

    # 2. Check symbol exists
    logger.debug(f"Looking up symbol {config.symbol}...")
    symbol_info = await asyncio.to_thread(mt5.symbol_info, config.symbol)
    if symbol_info is None:
        print(f"Symbol {config.symbol} not found")
        sys.exit(1)

    logger.debug(
        f"Symbol {config.symbol}: point={symbol_info.point}, "
        f"digits={symbol_info.digits}, "
        f"volume_min={symbol_info.volume_min}, "
        f"volume_max={symbol_info.volume_max}, "
        f"volume_step={symbol_info.volume_step}"
    )

    # 3. Ensure symbol is visible
    if not symbol_info.visible:
        selected = await asyncio.to_thread(mt5.symbol_select, config.symbol, True)
        if not selected:
            print(f"Failed to select symbol {config.symbol}")
            sys.exit(1)
        symbol_info = await asyncio.to_thread(mt5.symbol_info, config.symbol)

    logger.debug("Lot validation...")
    # 4. Lot validation
    if config.lots < symbol_info.volume_min:
        print(f"Lots {config.lots} below minimum {symbol_info.volume_min}")
        sys.exit(1)
    if config.lots > symbol_info.volume_max:
        print(f"Lots {config.lots} above maximum {symbol_info.volume_max}")
        sys.exit(1)
    remainder = (config.lots - symbol_info.volume_min) % symbol_info.volume_step
    if not math.isclose(remainder, 0, abs_tol=1e-9) and not math.isclose(
        remainder, symbol_info.volume_step, abs_tol=1e-9
    ):
        print(
            f"Lots {config.lots} not aligned to volume step {symbol_info.volume_step}"
        )
        sys.exit(1)

    # 5. Partial close validation
    if config.partial_close_pct > 0:
        partial_lots = round_to_lot_step(
            config.lots * config.partial_close_pct / 100,
            symbol_info.volume_min,
            symbol_info.volume_step,
        )
        remainder_lots = config.lots - partial_lots
        if partial_lots < symbol_info.volume_min or remainder_lots < symbol_info.volume_min:
            logger.warning(
                f"Partial close disabled: partial_lots={partial_lots}, "
                f"remainder={remainder_lots}, volume_min={symbol_info.volume_min}"
            )
            config.partial_close_pct = 0

    logger.debug("Checking margin via order_check...")
    # 6. Margin check via order_check
    tick = await asyncio.to_thread(mt5.symbol_info_tick, config.symbol)
    order_type = (
        mt5.ORDER_TYPE_BUY if config.trade_type == "buy" else mt5.ORDER_TYPE_SELL
    )
    price = tick.ask if config.trade_type == "buy" else tick.bid
    check_request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": config.symbol,
        "volume": config.lots,
        "type": order_type,
        "price": price,
        "deviation": config.deviation,
        "magic": config.magic,
        "comment": "scalper_check",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }
    check_result = await asyncio.to_thread(mt5.order_check, check_request)
    if check_result is None or check_result.retcode != 0:
        print(
            f"Order check failed: insufficient margin or invalid parameters. "
            f"Result: {check_result}"
        )
        sys.exit(1)
    logger.debug(
        f"Margin check passed: margin={check_result.margin:.2f}, "
        f"margin_free={check_result.margin_free:.2f}, "
        f"balance={check_result.balance:.2f}"
    )

    # 7. Check market is open
    if symbol_info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
        print(
            f"Market not open for full trading. trade_mode={symbol_info.trade_mode}"
        )
        sys.exit(1)

    logger.info("All validation checks passed")
    return symbol_info


async def spread_gate(
    config: TradeConfig, symbol_info, logger: logging.Logger
) -> None:
    """Wait for spread to normalize before entry. Aborts after 10 seconds."""
    tick = await asyncio.to_thread(mt5.symbol_info_tick, config.symbol)
    current_spread = (tick.ask - tick.bid) / symbol_info.point
    logger.debug(f"Pre-entry spread: {current_spread:.0f} (max: {config.max_spread})")

    if current_spread <= config.max_spread:
        return

    logger.info(
        f"Spread too wide for entry ({current_spread:.0f} > {config.max_spread}), "
        f"waiting..."
    )

    deadline = time.time() + 10
    while time.time() < deadline:
        await asyncio.sleep(0.2)
        tick = await asyncio.to_thread(mt5.symbol_info_tick, config.symbol)
        current_spread = (tick.ask - tick.bid) / symbol_info.point
        logger.debug(
            f"Spread gate poll: {current_spread:.0f} "
            f"(remaining: {deadline - time.time():.1f}s)"
        )
        if current_spread <= config.max_spread:
            logger.info(f"Spread normalized to {current_spread:.0f}")
            return

    print("Spread did not normalize within 10 seconds. Aborting entry.")
    sys.exit(1)


async def close_position(
    config: TradeConfig,
    trade_state: TradeState,
    symbol_info,
    logger: logging.Logger,
    volume: float | None = None,
) -> tuple[bool, float, float]:
    """Close position with retry logic.

    Returns (success, fill_price, requested_price).
    """
    lots = volume if volume is not None else trade_state.remaining_lots
    logger.info(
        f"Closing position: ticket={trade_state.ticket}, "
        f"lots={lots}, reason={trade_state.exit_reason}"
    )

    for attempt in range(3):
        tick = await asyncio.to_thread(mt5.symbol_info_tick, config.symbol)
        price = tick.bid if config.trade_type == "buy" else tick.ask
        deviation = config.deviation + (attempt * 10)

        if attempt < 2:
            filling = mt5.ORDER_FILLING_FOK
        else:
            filling = mt5.ORDER_FILLING_IOC

        filling_name = "FOK" if attempt < 2 else "IOC"
        logger.debug(
            f"Close attempt {attempt + 1}/3: price={price}, "
            f"deviation={deviation}, filling={filling_name}"
        )

        close_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": config.symbol,
            "volume": lots,
            "type": (
                mt5.ORDER_TYPE_SELL
                if config.trade_type == "buy"
                else mt5.ORDER_TYPE_BUY
            ),
            "position": trade_state.ticket,
            "price": price,
            "deviation": deviation,
            "magic": config.magic,
            "comment": f"close_{trade_state.exit_reason}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        result = await asyncio.to_thread(mt5.order_send, close_request)

        if result is not None and result.retcode == 10009:
            logger.info(
                f"Close filled: price={result.price}, "
                f"slippage={(abs(result.price - price) / symbol_info.point):.0f}pts"
            )
            return True, result.price, price

        error = await asyncio.to_thread(mt5.last_error)
        logger.warning(
            f"Close attempt {attempt + 1}/3 failed: "
            f"retcode={result.retcode if result else 'None'}, error={error}"
        )

        if attempt < 2:
            await asyncio.sleep(0.1)

    # Emergency close
    logger.critical("All normal close attempts failed, executing emergency close")
    tick = await asyncio.to_thread(mt5.symbol_info_tick, config.symbol)
    price = tick.bid if config.trade_type == "buy" else tick.ask
    logger.debug(
        f"Emergency close: price={price}, "
        f"deviation={config.emergency_deviation}, filling=RETURN"
    )

    emergency_request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": config.symbol,
        "volume": lots,
        "type": (
            mt5.ORDER_TYPE_SELL
            if config.trade_type == "buy"
            else mt5.ORDER_TYPE_BUY
        ),
        "position": trade_state.ticket,
        "price": price,
        "deviation": config.emergency_deviation,
        "magic": config.magic,
        "comment": f"emergency_close_{trade_state.exit_reason}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN,
    }

    result = await asyncio.to_thread(mt5.order_send, emergency_request)

    if result is not None and result.retcode == 10009:
        logger.info(f"Emergency close filled: price={result.price}")
        return True, result.price, price

    error = await asyncio.to_thread(mt5.last_error)
    logger.critical(
        f"Emergency close FAILED: "
        f"retcode={result.retcode if result else 'None'}, error={error}"
    )
    print(
        f"CRITICAL: POSITION {trade_state.ticket} STILL OPEN. "
        f"MANUAL INTERVENTION REQUIRED."
    )
    return False, 0.0, 0.0


async def verify_close(
    config: TradeConfig,
    trade_state: TradeState,
    symbol_info,
    fill_price: float,
    requested_price: float,
    logger: logging.Logger,
) -> bool:
    """Verify position is fully closed after a successful close order."""
    logger.debug("Verifying position closed (100ms wait)...")
    await asyncio.sleep(0.1)
    remaining = await asyncio.to_thread(
        mt5.positions_get, ticket=trade_state.ticket
    )

    if remaining is not None and len(remaining) > 0:
        logger.warning("Position still open after close confirmation, retrying...")
        success, new_fill, new_req = await close_position(
            config, trade_state, symbol_info, logger
        )
        if success:
            fill_price = new_fill
            requested_price = new_req
        else:
            return False

    logger.debug("Position confirmed closed")

    # Record exit slippage
    exit_slippage = abs(fill_price - requested_price) / symbol_info.point
    trade_state.exit_slippage_points = exit_slippage
    logger.debug(f"Exit slippage: {exit_slippage:.0f} points")
    if exit_slippage > config.max_exit_slippage:
        logger.warning(
            f"Exit slippage {exit_slippage:.0f} points exceeds max "
            f"{config.max_exit_slippage}"
        )

    return True


async def attempt_reconnect(
    trade_state: TradeState, logger: logging.Logger
) -> bool:
    """Attempt to reconnect to MT5 with exponential backoff (5 attempts)."""
    disconnect_start = time.time()

    for attempt in range(5):
        wait = 2**attempt  # 1, 2, 4, 8, 16
        logger.warning(f"Reconnect attempt {attempt + 1}/5, waiting {wait}s...")
        await asyncio.sleep(wait)
        await asyncio.to_thread(mt5.shutdown)
        if await asyncio.to_thread(mt5.initialize):
            elapsed = time.time() - disconnect_start
            trade_state.total_disconnect_seconds += elapsed
            logger.info(f"Reconnected after {elapsed:.1f}s")
            return True

    elapsed = time.time() - disconnect_start
    trade_state.total_disconnect_seconds += elapsed
    logger.critical("Reconnect failed after 5 attempts")
    return False


async def trade_loop(
    config: TradeConfig,
    trade_state: TradeState,
    tick_buffer: collections.deque,
    stop_event: asyncio.Event,
    symbol_info,
    pnl_per_point: float,
    logger: logging.Logger,
) -> None:
    """Combined tick collector and position monitor. Runs at ~50ms intervals."""
    last_tick_time = 0.0
    last_tick_bid = 0.0
    last_tick_ask = 0.0
    last_new_tick_real_time = time.time()
    iteration = 0
    previous_pnl = 0.0
    last_peak_milestone = 0.0
    tick_drought_warned = False
    loss_80pct_warned = False
    warmup_logged = False

    stop_file = Path(f".close_{config.magic}")
    # Remove any stale stop-file left from a previous run
    stop_file.unlink(missing_ok=True)

    logger.info("Trade loop started, monitoring position...")

    while not stop_event.is_set():
        iter_start = time.time()
        iteration += 1

        # Check for UI-initiated close (stop-file written by the UI dashboard)
        if stop_file.exists():
            logger.info("Close requested via UI stop-file")
            stop_file.unlink(missing_ok=True)
            if not trade_state.exit_reason:
                trade_state.exit_reason = "manual_shutdown"
            stop_event.set()
            return

        # a) Poll tick
        tick = await asyncio.to_thread(mt5.symbol_info_tick, config.symbol)

        if tick is None:
            # Possible disconnect
            error = await asyncio.to_thread(mt5.last_error)
            logger.warning(f"symbol_info_tick returned None, error={error}")

            reconnected = await attempt_reconnect(trade_state, logger)

            if reconnected:
                # Check position still exists
                logger.debug("Checking position after reconnect...")
                positions = await asyncio.to_thread(
                    mt5.positions_get, ticket=trade_state.ticket
                )
                if positions is None or len(positions) == 0:
                    logger.warning(
                        f"Position {trade_state.ticket} closed during disconnect"
                    )
                    trade_state.exit_reason = "closed_during_disconnect"
                    stop_event.set()
                    return
                logger.info(
                    f"Position still open after reconnect: "
                    f"volume={positions[0].volume}, profit={positions[0].profit}"
                )
                continue
            else:
                # Final emergency close attempt
                trade_state.exit_reason = "disconnect_failed"
                stop_event.set()
                return

        # b) Check if tick is new
        is_new_tick = (
            tick.time != last_tick_time
            or tick.bid != last_tick_bid
            or tick.ask != last_tick_ask
        )

        if is_new_tick:
            last_tick_time = tick.time
            last_tick_bid = tick.bid
            last_tick_ask = tick.ask
            last_new_tick_real_time = time.time()
            tick_drought_warned = False

            tick_buffer.append(
                TickEntry(timestamp=time.time(), bid=tick.bid, ask=tick.ask)
            )
            trade_state.total_ticks += 1

            # c) Compute current P&L (exact via order_calc_profit)
            lots_for_pnl = (
                trade_state.remaining_lots
                if trade_state.partial_closed
                else config.lots
            )
            order_type = (
                mt5.ORDER_TYPE_BUY
                if config.trade_type == "buy"
                else mt5.ORDER_TYPE_SELL
            )
            close_price = (
                tick.bid if config.trade_type == "buy" else tick.ask
            )

            gross_pnl = await asyncio.to_thread(
                mt5.order_calc_profit,
                order_type,
                config.symbol,
                lots_for_pnl,
                trade_state.open_price,
                close_price,
            )

            if gross_pnl is None:
                logger.warning("order_calc_profit returned None")
                gross_pnl = trade_state.current_pnl  # Use last known net as fallback

            # Estimated round-trip commission: entry deal commission (negative) × 2.
            # This accounts for the exit deal commission we haven't been charged yet.
            # If entry_commission is 0 (fetch failed), net == gross — no regression.
            commission_estimate = trade_state.entry_commission * 2
            current_pnl = gross_pnl + commission_estimate

            trade_state.current_pnl = current_pnl
            current_loss = abs(min(0.0, current_pnl))
            trade_state.max_loss = max(trade_state.max_loss, current_loss)

            # Warn when loss exceeds 80% of hard stop
            if current_loss >= config.loss_max * 0.8 and not loss_80pct_warned:
                logger.warning(
                    f"Loss ${current_loss:.2f} exceeds 80% of hard stop "
                    f"${config.loss_max:.2f}"
                )
                loss_80pct_warned = True
            elif current_loss < config.loss_max * 0.8:
                loss_80pct_warned = False

            # Track P&L velocity and time allocation
            now = time.time()
            if trade_state.last_pnl_timestamp > 0:
                time_delta = now - trade_state.last_pnl_timestamp
                if time_delta > 0:
                    velocity = (current_pnl - previous_pnl) / time_delta
                    trade_state.max_profit_velocity = max(
                        trade_state.max_profit_velocity, velocity
                    )
                    trade_state.max_loss_velocity = min(
                        trade_state.max_loss_velocity, velocity
                    )

                    if current_pnl >= 0:
                        trade_state.time_in_profit += time_delta
                    else:
                        trade_state.time_in_loss += time_delta

            previous_pnl = current_pnl
            trade_state.last_pnl_timestamp = now

            # Tick interval measurement
            if trade_state.total_ticks > 1:
                tick_interval = now - (trade_state.last_pnl_timestamp if trade_state.last_pnl_timestamp != now else now)
                # Use time since previous tick was stored
                if len(tick_buffer) >= 2:
                    tick_interval_ms = (
                        tick_buffer[-1].timestamp - tick_buffer[-2].timestamp
                    ) * 1000
                    logger.debug(
                        f"P&L: ${current_pnl:.2f} | "
                        f"spread: {(tick.ask - tick.bid) / symbol_info.point:.0f} | "
                        f"tick_interval: {tick_interval_ms:.0f}ms | "
                        f"bid: {tick.bid} | ask: {tick.ask}"
                    )
                else:
                    logger.debug(
                        f"P&L: ${current_pnl:.2f} | "
                        f"spread: {(tick.ask - tick.bid) / symbol_info.point:.0f} | "
                        f"bid: {tick.bid} | ask: {tick.ask}"
                    )
            else:
                logger.debug(
                    f"P&L: ${current_pnl:.2f} | "
                    f"spread: {(tick.ask - tick.bid) / symbol_info.point:.0f} | "
                    f"bid: {tick.bid} | ask: {tick.ask} | first tick"
                )

            # === EXIT LOGIC ===
            elapsed = (
                now - trade_state.open_time - trade_state.total_disconnect_seconds
            )
            current_spread = (tick.ask - tick.bid) / symbol_info.point
            exit_reason = None

            # Step 0: Spread check
            spread_wide = current_spread > config.max_spread
            if spread_wide:
                logger.warning(
                    f"Spread {current_spread:.0f} exceeds max {config.max_spread}"
                )

            # Step 1: Hard stop (ALWAYS active, even during wide spread)
            if current_loss >= config.loss_max:
                exit_reason = "hard_stop"
            logger.debug(
                f"Exit eval: elapsed={elapsed:.1f}s, loss=${current_loss:.2f}, "
                f"spread={'WIDE' if spread_wide else 'OK'}"
            )

            if not exit_reason and not spread_wide:
                # Step 2: Max duration
                if elapsed >= config.max_duration:
                    exit_reason = "max_duration"
                    logger.debug(
                        f"Max duration reached: {elapsed:.1f}s >= "
                        f"{config.max_duration}s"
                    )

                # Step 3: Loss velocity (active from second 0, NOT suppressed by warmup)
                # Pass gross_pnl: approx_pnl() inside also uses gross (no commission),
                # so the drop calculation is consistent. Commission cancels in deltas.
                if not exit_reason:
                    exit_reason = _check_loss_velocity(
                        config,
                        trade_state,
                        tick_buffer,
                        symbol_info,
                        pnl_per_point,
                        gross_pnl,
                        now,
                        logger,
                    )

                # Warmup check — steps 4-9 suppressed during warmup
                in_warmup = elapsed < config.warmup

                if in_warmup:
                    logger.debug(
                        f"Warmup active: {elapsed:.1f}s / {config.warmup}s "
                        f"(steps 4-9 suppressed)"
                    )
                elif not warmup_logged:
                    logger.info(
                        f"Warmup period ended at {elapsed:.1f}s, "
                        f"all exit conditions now active"
                    )
                    warmup_logged = True

                if not in_warmup:
                    # Step 4: Breakeven stop
                    if (
                        not exit_reason
                        and trade_state.breakeven_activated
                        and not trade_state.profit_activated
                    ):
                        logger.debug(
                            f"Breakeven check: pnl=${current_pnl:.2f} vs "
                            f"buffer=${config.breakeven_buffer:.2f}"
                        )
                        if current_pnl < config.breakeven_buffer:
                            exit_reason = "breakeven_stop"

                    # Step 5: Profit trailing
                    if not exit_reason and trade_state.profit_activated:
                        trade_state.peak_profit = max(
                            trade_state.peak_profit, current_pnl
                        )

                        # Log peak milestones every $0.50
                        milestone = (
                            math.floor(trade_state.peak_profit / 0.50) * 0.50
                        )
                        if milestone > last_peak_milestone and milestone > 0:
                            logger.info(
                                f"New peak profit milestone: "
                                f"${trade_state.peak_profit:.2f}"
                            )
                            last_peak_milestone = milestone

                        pct_threshold = trade_state.peak_profit * (
                            1 - config.profit_trail_pct / 100
                        )
                        abs_threshold = (
                            trade_state.peak_profit - config.profit_trail_amount
                        )
                        threshold = max(pct_threshold, abs_threshold)

                        logger.debug(
                            f"Trailing: pnl=${current_pnl:.2f}, "
                            f"peak=${trade_state.peak_profit:.2f}, "
                            f"pct_thr=${pct_threshold:.2f}, "
                            f"abs_thr=${abs_threshold:.2f}, "
                            f"threshold=${threshold:.2f}"
                        )

                        if current_pnl <= threshold:
                            exit_reason = "profit_trailing"

                    # Step 6: Profit activation
                    if (
                        not trade_state.profit_activated
                        and current_pnl >= config.profit_min
                    ):
                        trade_state.profit_activated = True
                        trade_state.peak_profit = current_pnl
                        trade_state.profit_activation_time = now
                        logger.info(
                            f"Profit target activated at ${current_pnl:.2f}"
                        )

                    # Step 7: Breakeven activation
                    if (
                        not trade_state.breakeven_activated
                        and current_pnl >= config.breakeven_at
                    ):
                        trade_state.breakeven_activated = True
                        logger.info(
                            f"Breakeven stop activated at ${current_pnl:.2f}"
                        )

                    # Step 8: Partial close
                    if (
                        not exit_reason
                        and config.partial_close_pct > 0
                        and not trade_state.partial_closed
                        and current_pnl >= config.profit_min
                    ):
                        await _execute_partial_close(
                            config,
                            trade_state,
                            symbol_info,
                            current_pnl,
                            logger,
                        )

                    # Step 9: Profit protection (safety net)
                    if (
                        not exit_reason
                        and trade_state.profit_activated
                        and current_pnl <= 0
                    ):
                        logger.warning(
                            f"Profit evaporated: was activated at "
                            f"${config.profit_min:.2f}, now ${current_pnl:.2f}"
                        )
                        exit_reason = "profit_evaporated"

            # Handle exit
            if exit_reason:
                trade_state.exit_reason = exit_reason
                logger.info(f"Exit triggered: {exit_reason}")
                stop_event.set()
                return

        else:
            # Not a new tick — check for tick drought
            drought_duration = time.time() - last_new_tick_real_time
            if drought_duration > 2.0 and trade_state.current_pnl < 0:
                logger.warning(
                    f"Tick drought emergency: {drought_duration:.1f}s "
                    f"with negative P&L"
                )
                trade_state.exit_reason = "tick_drought_emergency"
                stop_event.set()
                return
            elif drought_duration > 0.5 and not tick_drought_warned:
                logger.warning(
                    f"Tick drought: {drought_duration:.1f}s since last new tick"
                )
                tick_drought_warned = True

        # d) Position existence check every 100 iterations
        if iteration % 100 == 0:
            logger.debug(
                f"Position existence check (iteration {iteration})"
            )
            positions = await asyncio.to_thread(
                mt5.positions_get, ticket=trade_state.ticket
            )
            if positions is None or len(positions) == 0:
                logger.info(
                    f"Position {trade_state.ticket} no longer exists "
                    f"(closed externally)"
                )
                trade_state.exit_reason = "external_close"
                stop_event.set()
                return
            else:
                logger.debug(
                    f"Position confirmed: volume={positions[0].volume}, "
                    f"profit={positions[0].profit}"
                )

        # e) Adaptive sleep to maintain ~50ms interval
        iter_duration = time.time() - iter_start
        if iter_duration > 0.050:
            logger.warning(
                f"Tick poll took {iter_duration * 1000:.0f}ms (>50ms)"
            )
        sleep_time = max(0, 0.050 - iter_duration)
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)


def _check_loss_velocity(
    config: TradeConfig,
    trade_state: TradeState,
    tick_buffer: collections.deque,
    symbol_info,
    pnl_per_point: float,
    current_pnl: float,
    now: float,
    logger: logging.Logger,
) -> str | None:
    """Check loss velocity using both directional and drawdown modes.

    Returns exit reason string or None.
    """
    window_start = now - config.loss_velocity_window

    # Collect ticks in window (iterate from newest)
    window_ticks: list[TickEntry] = []
    for te in reversed(tick_buffer):
        if te.timestamp < window_start:
            break
        window_ticks.append(te)
    window_ticks.reverse()

    if len(window_ticks) < 2:
        return None

    # Adjust pnl_per_point for partial close
    lots_ratio = (
        trade_state.remaining_lots / config.lots
        if trade_state.partial_closed
        else 1.0
    )
    adj_pnl_per_point = pnl_per_point * lots_ratio

    def approx_pnl(te: TickEntry) -> float:
        tp = te.bid if config.trade_type == "buy" else te.ask
        return (tp - trade_state.open_price) / symbol_info.point * adj_pnl_per_point

    # Mode A: Directional velocity (start-to-end of window)
    pnl_start = approx_pnl(window_ticks[0])
    directional_drop = pnl_start - current_pnl

    # Mode B: Peak drawdown within window
    max_pnl_in_window = max(approx_pnl(te) for te in window_ticks)
    drawdown_in_window = max_pnl_in_window - current_pnl

    logger.debug(
        f"Loss velocity: directional_drop=${directional_drop:.2f}, "
        f"drawdown=${drawdown_in_window:.2f}, "
        f"threshold=${config.loss_velocity:.2f}"
    )

    if directional_drop >= config.loss_velocity and current_pnl < 0:
        logger.info(
            f"Loss velocity triggered (Mode A): "
            f"directional_drop=${directional_drop:.2f}"
        )
        return "loss_velocity"

    if drawdown_in_window >= config.loss_velocity:
        logger.info(
            f"Loss velocity triggered (Mode B): "
            f"drawdown=${drawdown_in_window:.2f}"
        )
        return "loss_velocity"

    return None


async def _execute_partial_close(
    config: TradeConfig,
    trade_state: TradeState,
    symbol_info,
    current_pnl: float,
    logger: logging.Logger,
) -> None:
    """Execute a partial close of the position."""
    partial_lots = round_to_lot_step(
        config.lots * config.partial_close_pct / 100,
        symbol_info.volume_min,
        symbol_info.volume_step,
    )

    if partial_lots < symbol_info.volume_min:
        logger.warning(
            f"Partial close lot size {partial_lots} below minimum, skipping"
        )
        trade_state.partial_closed = True
        return

    tick = await asyncio.to_thread(mt5.symbol_info_tick, config.symbol)
    price = tick.bid if config.trade_type == "buy" else tick.ask

    partial_request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": config.symbol,
        "volume": partial_lots,
        "type": (
            mt5.ORDER_TYPE_SELL
            if config.trade_type == "buy"
            else mt5.ORDER_TYPE_BUY
        ),
        "position": trade_state.ticket,
        "price": price,
        "deviation": config.deviation,
        "magic": config.magic,
        "comment": "partial_close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }

    result = await asyncio.to_thread(mt5.order_send, partial_request)

    if result is not None and result.retcode == 10009:
        # Verify by checking remaining volume
        await asyncio.sleep(0.1)
        positions = await asyncio.to_thread(
            mt5.positions_get, ticket=trade_state.ticket
        )
        if positions and len(positions) > 0:
            trade_state.remaining_lots = positions[0].volume
        else:
            trade_state.remaining_lots = config.lots - partial_lots

        trade_state.partial_closed = True
        trade_state.partial_close_pnl = current_pnl
        logger.info(
            f"Partial close: {partial_lots} lots at ${current_pnl:.2f}, "
            f"remaining: {trade_state.remaining_lots} lots"
        )
    else:
        error = await asyncio.to_thread(mt5.last_error)
        logger.warning(f"Partial close failed: {error}")
        trade_state.partial_closed = True  # Don't retry every cycle


async def run() -> None:
    """Main coroutine: parse args, validate, open position, monitor, close."""
    config = parse_args()
    logger = setup_logging(config.symbol, config.trade_type)

    logger.info(
        f"Starting scalper: {config.symbol} {config.trade_type} {config.lots} lots"
    )
    logger.debug(
        f"Config: profit_min={config.profit_min}, loss_max={config.loss_max}, "
        f"trail_pct={config.profit_trail_pct}%, trail_amt={config.profit_trail_amount}, "
        f"loss_velocity={config.loss_velocity}/{config.loss_velocity_window}s, "
        f"max_spread={config.max_spread}, warmup={config.warmup}s, "
        f"max_duration={config.max_duration}s, breakeven_at={config.breakeven_at}, "
        f"breakeven_buffer={config.breakeven_buffer}, "
        f"partial_close_pct={config.partial_close_pct}%, "
        f"entry_slip={config.max_entry_slippage}, exit_slip={config.max_exit_slippage}, "
        f"deviation={config.deviation}, emergency_dev={config.emergency_deviation}, "
        f"magic={config.magic}"
    )

    # Validate
    symbol_info = await validate(config, logger)

    # Check for existing positions on this symbol
    positions = await asyncio.to_thread(mt5.positions_get, symbol=config.symbol)
    managing_existing = False

    if positions is not None and len(positions) > 0:
        print(f"\nExisting positions on {config.symbol}:")
        for pos in positions:
            pos_type = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
            print(
                f"  Ticket: {pos.ticket} | {pos_type} | {pos.volume} lots | "
                f"Open: {pos.price_open} | P&L: {pos.profit}"
            )

        choice = await asyncio.to_thread(
            input,
            "\nEnter ticket number to manage existing, or 'new' to open new: ",
        )

        if choice.strip().lower() != "new":
            ticket = int(choice.strip())
            target_pos = None
            for pos in positions:
                if pos.ticket == ticket:
                    target_pos = pos
                    break

            if target_pos is None:
                print(f"Ticket {ticket} not found")
                sys.exit(1)

            managing_existing = True
            trade_state = TradeState(
                ticket=target_pos.ticket,
                open_price=target_pos.price_open,
                open_time=time.time(),
                remaining_lots=target_pos.volume,
            )
            config.trade_type = (
                "buy" if target_pos.type == mt5.ORDER_TYPE_BUY else "sell"
            )
            config.lots = target_pos.volume
            logger.info(
                f"Managing existing position: ticket={ticket}, "
                f"type={config.trade_type}, lots={config.lots}"
            )

    if not managing_existing:
        # Spread gate
        await spread_gate(config, symbol_info, logger)

        # Open position
        logger.info("Opening position...")
        tick = await asyncio.to_thread(mt5.symbol_info_tick, config.symbol)
        order_type = (
            mt5.ORDER_TYPE_BUY
            if config.trade_type == "buy"
            else mt5.ORDER_TYPE_SELL
        )
        requested_price = tick.ask if config.trade_type == "buy" else tick.bid

        open_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": config.symbol,
            "volume": config.lots,
            "type": order_type,
            "price": requested_price,
            "deviation": config.deviation,
            "magic": config.magic,
            "comment": "scalper_entry",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
        }

        logger.debug(
            f"Entry order: {config.trade_type.upper()} {config.lots} lots "
            f"@ {requested_price}, deviation={config.deviation}"
        )
        result = await asyncio.to_thread(mt5.order_send, open_request)

        if result is None or result.retcode != 10009:
            error = await asyncio.to_thread(mt5.last_error)
            print(
                f"Order send failed: "
                f"retcode={result.retcode if result else 'None'}, error={error}"
            )
            await asyncio.to_thread(mt5.shutdown)
            sys.exit(1)

        # Record fill and compute entry slippage
        slippage_points = (
            abs(result.price - requested_price) / symbol_info.point
        )

        trade_state = TradeState(
            ticket=result.order,
            open_price=result.price,
            open_time=time.time(),
            remaining_lots=config.lots,
            entry_slippage_points=slippage_points,
        )

        logger.info(
            f"Position opened: ticket={trade_state.ticket}, "
            f"price={trade_state.open_price}, lots={config.lots}, "
            f"entry_slippage={slippage_points:.0f}pts"
        )

        # Fetch entry deal commission from history.
        # Wait briefly — the deal may not appear in MT5 history instantly.
        await asyncio.sleep(0.3)
        entry_deals = await asyncio.to_thread(
            mt5.history_deals_get, position=trade_state.ticket
        )
        if entry_deals:
            trade_state.entry_commission = sum(d.commission for d in entry_deals)
            logger.info(
                f"Entry commission: ${trade_state.entry_commission:.2f} "
                f"({len(entry_deals)} deal(s))"
            )
        else:
            logger.warning(
                "Could not retrieve entry deal from history — "
                "commission estimate will be zero"
            )

        # Reject if entry slippage too high
        if slippage_points > config.max_entry_slippage:
            logger.warning(
                f"Entry slippage {slippage_points:.0f} exceeds max "
                f"{config.max_entry_slippage}, closing immediately"
            )
            trade_state.exit_reason = "entry_slippage_rejected"
            success, fill_price, req_price = await close_position(
                config, trade_state, symbol_info, logger
            )
            if success:
                await verify_close(
                    config, trade_state, symbol_info, fill_price, req_price,
                    logger,
                )
            await asyncio.to_thread(mt5.shutdown)
            return

    # Compute pnl_per_point for fast approximate P&L in loss velocity
    reference_price = trade_state.open_price + 100 * symbol_info.point
    order_type = (
        mt5.ORDER_TYPE_BUY
        if config.trade_type == "buy"
        else mt5.ORDER_TYPE_SELL
    )
    reference_pnl = await asyncio.to_thread(
        mt5.order_calc_profit,
        order_type,
        config.symbol,
        config.lots,
        trade_state.open_price,
        reference_price,
    )

    if reference_pnl is None or reference_pnl == 0:
        logger.warning("Could not compute pnl_per_point, using fallback")
        pnl_per_point = 0.01
    else:
        pnl_per_point = reference_pnl / 100.0  # P&L per 1 point

    logger.debug(f"pnl_per_point = {pnl_per_point:.6f}")

    # Setup
    stop_event = asyncio.Event()
    tick_buffer: collections.deque[TickEntry] = collections.deque(maxlen=6000)

    # Signal handling
    loop = asyncio.get_running_loop()

    def on_shutdown() -> None:
        logger.info("Shutdown signal received, stopping trade loop...")
        if not trade_state.exit_reason:
            trade_state.exit_reason = "manual_shutdown"
        stop_event.set()

    if sys.platform == "win32":

        def signal_handler(sig, frame):
            loop.call_soon_threadsafe(on_shutdown)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    else:
        loop.add_signal_handler(signal.SIGINT, on_shutdown)
        loop.add_signal_handler(signal.SIGTERM, on_shutdown)

    # Run trade loop with exception safety
    exit_price = 0.0

    try:
        trade_task = asyncio.create_task(
            trade_loop(
                config, trade_state, tick_buffer, stop_event, symbol_info,
                pnl_per_point, logger,
            )
        )

        # Wait for trade loop to complete
        await trade_task

        # Close position (unless already gone)
        logger.info(
            f"Trade loop ended: reason={trade_state.exit_reason}, "
            f"pnl=${trade_state.current_pnl:.2f}"
        )
        if trade_state.exit_reason not in (
            "external_close",
            "closed_during_disconnect",
        ):
            # For disconnect_failed, attempt one final emergency close
            if trade_state.exit_reason == "disconnect_failed":
                # Re-initialize for emergency close attempt
                if await asyncio.to_thread(mt5.initialize):
                    trade_state.exit_reason = "disconnect_failed"
                    success, fill, req = await close_position(
                        config, trade_state, symbol_info, logger
                    )
                    if success:
                        exit_price = fill
                        await verify_close(
                            config, trade_state, symbol_info, fill, req,
                            logger,
                        )
                else:
                    print(
                        f"CRITICAL: UNABLE TO CLOSE POSITION "
                        f"{trade_state.ticket}. MANUAL INTERVENTION REQUIRED."
                    )
            else:
                success, fill, req = await close_position(
                    config, trade_state, symbol_info, logger
                )
                if success:
                    exit_price = fill
                    await verify_close(
                        config, trade_state, symbol_info, fill, req, logger
                    )

    except Exception:
        logger.critical("Unhandled exception", exc_info=True)
        # Attempt emergency close before re-raising
        if trade_state.ticket and trade_state.exit_reason not in (
            "external_close",
            "closed_during_disconnect",
        ):
            if not trade_state.exit_reason:
                trade_state.exit_reason = "unhandled_exception"
            try:
                success, fill, req = await close_position(
                    config, trade_state, symbol_info, logger
                )
                if success:
                    exit_price = fill
            except Exception:
                logger.critical(
                    "Failed to close position during exception handling",
                    exc_info=True,
                )
        raise

    finally:
        # ── Fetch authoritative P&L from server deal history ──────────────────
        # order_calc_profit() uses client-side tick prices and excludes commission.
        # The actual fill price and all charges come from the closed deals.
        server_gross_pnl = trade_state.current_pnl  # fallback if history unavailable
        total_commission = trade_state.entry_commission  # minimum we know about
        total_swap = 0.0

        await asyncio.sleep(0.3)  # allow server to settle deals into history
        try:
            all_deals = await asyncio.to_thread(
                mt5.history_deals_get, position=trade_state.ticket
            )
            if all_deals and len(all_deals) > 0:
                server_gross_pnl   = sum(d.profit     for d in all_deals)
                total_commission   = sum(d.commission for d in all_deals)
                total_swap         = sum(d.swap       for d in all_deals)
                logger.debug(
                    f"Deal history: {len(all_deals)} deals | "
                    f"gross=${server_gross_pnl:.2f} | "
                    f"commission=${total_commission:.2f} | "
                    f"swap=${total_swap:.2f}"
                )
            else:
                logger.warning(
                    "history_deals_get returned no deals — "
                    "final_pnl will use last known estimate"
                )
        except Exception as e:
            logger.warning(f"Failed to fetch deal history: {e}")

        trade_state.total_commission = total_commission
        trade_state.total_swap       = total_swap
        final_net_pnl = server_gross_pnl + total_commission + total_swap

        # ── Compute summary metrics ───────────────────────────────────────────
        duration = (
            time.time()
            - trade_state.open_time
            - trade_state.total_disconnect_seconds
        )
        avg_tick_ms = (
            (duration / trade_state.total_ticks * 1000)
            if trade_state.total_ticks > 0
            else 0
        )

        if (
            trade_state.profit_activated
            and trade_state.profit_activation_time > 0
        ):
            tta = (
                trade_state.profit_activation_time
                - trade_state.open_time
                - trade_state.total_disconnect_seconds
            )
            time_to_activation = f"{tta:.1f}s"
        else:
            time_to_activation = "N/A"

        logger.info(
            f"=== TRADE SUMMARY ===\n"
            f"  reason: {trade_state.exit_reason}\n"
            f"  duration_seconds: {duration:.1f}\n"
            f"  entry_price: {trade_state.open_price}\n"
            f"  exit_price: {exit_price}\n"
            f"  entry_slippage_points: "
            f"{trade_state.entry_slippage_points:.0f}\n"
            f"  exit_slippage_points: "
            f"{trade_state.exit_slippage_points:.0f}\n"
            f"  gross_pnl: ${server_gross_pnl:.2f}\n"
            f"  commission: ${total_commission:.2f}\n"
            f"  swap: ${total_swap:.2f}\n"
            f"  final_pnl: ${final_net_pnl:.2f}\n"
            f"  peak_profit: ${trade_state.peak_profit:.2f}\n"
            f"  max_loss: ${trade_state.max_loss:.2f}\n"
            f"  total_ticks: {trade_state.total_ticks}\n"
            f"  average_tick_interval_ms: {avg_tick_ms:.1f}\n"
            f"  time_in_profit_seconds: "
            f"{trade_state.time_in_profit:.1f}\n"
            f"  time_in_loss_seconds: {trade_state.time_in_loss:.1f}\n"
            f"  max_profit_velocity: "
            f"${trade_state.max_profit_velocity:.2f}/sec\n"
            f"  max_loss_velocity: "
            f"${trade_state.max_loss_velocity:.2f}/sec\n"
            f"  time_to_profit_activation: {time_to_activation}\n"
            f"  partial_close_executed: {trade_state.partial_closed}\n"
            f"  partial_close_pnl: ${trade_state.partial_close_pnl:.2f}\n"
            f"  breakeven_activated: {trade_state.breakeven_activated}\n"
            f"  total_disconnect_seconds: "
            f"{trade_state.total_disconnect_seconds:.1f}"
        )

        await asyncio.to_thread(mt5.shutdown)


def main() -> None:
    """Entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
