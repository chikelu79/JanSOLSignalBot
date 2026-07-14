import asyncio
import logging
import os
from contextlib import suppress
from typing import Any

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

import market
from bot_state import (
    add_to_watchlist,
    get_runtime_chat_id,
    get_selected_pair,
    get_state_snapshot,
    get_watchlist,
    is_monitor_enabled,
    remove_from_watchlist,
    set_monitor_enabled,
    set_runtime_chat_id,
    set_selected_pair,
    set_watchlist,
)
from notifier import (
    build_active_setups_message,
    build_scan_message,
    evaluate_signal_alert,
    evaluate_derivatives_alert,
    price_text,
)
from strategy import (
    MarketSignal,
    build_market_signal,
)


# =========================================================
# OPTIONAL MARKET CONTEXT
# =========================================================

try:
    from market_context import (
    MarketContext,
    build_market_context,
    get_market_context_data,
    fetch_derivatives_context,
)

    MARKET_CONTEXT_AVAILABLE = True

except ImportError:
    MarketContext = Any
    MARKET_CONTEXT_AVAILABLE = False

    def build_market_context(
        selected_signal: MarketSignal,
        context_data: dict[str, Any],
    ) -> None:
        return None

    async def fetch_derivatives_context(symbol: str) -> dict[str, Any] | None:
        return None


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format=(
        "%(asctime)s | %(levelname)s | "
        "%(name)s | %(message)s"
    ),
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# ENVIRONMENT
# =========================================================

# Removes accidental spaces, carriage returns and newlines.
TELEGRAM_TOKEN = "".join(
    os.getenv(
        "TELEGRAM_BOT_TOKEN",
        "",
    ).split()
)

ENVIRONMENT_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "",
).strip()

MONITOR_INTERVAL_SECONDS = int(
    os.getenv(
        "MONITOR_INTERVAL_SECONDS",
        "30",
    )
)

INITIAL_MONITOR_DELAY_SECONDS = int(
    os.getenv(
        "INITIAL_MONITOR_DELAY_SECONDS",
        "15",
    )
)

MAX_MONITORED_PAIRS = int(
    os.getenv(
        "MAX_MONITORED_PAIRS",
        "8",
    )
)

SCAN_CONCURRENCY = int(
    os.getenv(
        "SCAN_CONCURRENCY",
        "2",
    )
)


# =========================================================
# TELEGRAM MESSAGE HELPERS
# =========================================================

TELEGRAM_MESSAGE_LIMIT = 3900


async def send_long_message(
    update: Update,
    text: str,
) -> None:
    message = update.effective_message

    if message is None:
        return

    remaining = text

    while remaining:
        if len(remaining) <= TELEGRAM_MESSAGE_LIMIT:
            chunk = remaining
            remaining = ""

        else:
            split_at = remaining.rfind(
                "\n",
                0,
                TELEGRAM_MESSAGE_LIMIT,
            )

            if split_at <= 0:
                split_at = TELEGRAM_MESSAGE_LIMIT

            chunk = remaining[:split_at]
            remaining = remaining[
                split_at:
            ].lstrip()

        await message.reply_text(
            chunk
        )


async def edit_or_reply(
    update: Update,
    waiting_message: Any,
    text: str,
) -> None:
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        try:
            await waiting_message.edit_text(
                text
            )

            return

        except Exception:
            logger.exception(
                "Could not edit Telegram message."
            )

    try:
        await waiting_message.delete()

    except Exception:
        pass

    await send_long_message(
        update,
        text,
    )


def get_destination_chat_id() -> str:
    runtime_chat_id = (
        get_runtime_chat_id()
    )

    if runtime_chat_id:
        return runtime_chat_id

    return ENVIRONMENT_CHAT_ID


# =========================================================
# MARKET COMPATIBILITY HELPERS
# =========================================================

async def validate_market_symbol(
    symbol: str,
) -> None:
    validator = getattr(
        market,
        "validate_symbol",
        None,
    )

    if validator is None:
        return

    await validator(
        symbol
    )


async def fetch_ticker(
    symbol: str,
) -> dict[str, Any]:
    ticker_function = getattr(
        market,
        "get_ticker_24h",
        None,
    )

    if ticker_function is None:
        raise RuntimeError(
            "market.py does not contain "
            "get_ticker_24h()."
        )

    try:
        return await ticker_function(
            symbol
        )

    except TypeError:
        # Compatibility with an older hardcoded market.py.
        return await ticker_function()


async def fetch_symbol_snapshot(
    symbol: str,
) -> dict[str, Any]:
    snapshot_function = getattr(
        market,
        "get_symbol_snapshot",
        None,
    )

    if snapshot_function is not None:
        return await snapshot_function(
            symbol
        )

    legacy_snapshot_function = getattr(
        market,
        "get_market_snapshot",
        None,
    )

    if legacy_snapshot_function is not None:
        try:
            result = await legacy_snapshot_function(
                symbol
            )

        except TypeError:
            result = await legacy_snapshot_function()

        if isinstance(
            result,
            dict,
        ):
            result.setdefault(
                "symbol",
                symbol,
            )

            return result

    timeframe_function = getattr(
        market,
        "get_all_timeframes",
        None,
    )

    if timeframe_function is None:
        raise RuntimeError(
            "market.py does not contain a usable "
            "snapshot or timeframe function."
        )

    try:
        candles, errors = (
            await timeframe_function(
                symbol
            )
        )

    except TypeError:
        candles, errors = (
            await timeframe_function()
        )

    ticker = await fetch_ticker(
        symbol
    )

    return {
        "symbol": symbol,
        "ticker": ticker,
        "candles": candles,
        "errors": errors,
    }


async def fetch_context_data(
    symbol: str,
) -> dict[str, Any] | None:
    if not MARKET_CONTEXT_AVAILABLE:
        return None

    try:
        return await get_market_context_data(
            symbol
        )

    except Exception:
        logger.exception(
            "Macro market data failed for %s.",
            symbol,
        )

        return None
    

# =========================================================
# ANALYSIS ENGINE
# =========================================================

async def analyze_symbol(
    symbol: str,
    include_context: bool = True,
) -> tuple[
    MarketSignal,
    Any | None,
    dict[str, Any],
]:
    snapshot = await fetch_symbol_snapshot(
        symbol
    )

    candles = snapshot.get(
        "candles",
        {},
    )

    errors = snapshot.get(
        "errors",
        {},
    )

    if not candles:
        raise RuntimeError(
            f"No candle data were returned for {symbol}."
        )

    signal = await asyncio.to_thread(
        build_market_signal,
        symbol,
        candles,
        errors,
    )

    market_context = None

    if (
        include_context
        and MARKET_CONTEXT_AVAILABLE
    ):
        context_data = await fetch_context_data(
            symbol
        )

        if context_data:
            market_context = (
                await asyncio.to_thread(
                    build_market_context,
                    signal,
                    context_data,
                )
            )

    return (
        signal,
        market_context,
        snapshot,
    )


def adjusted_score(
    signal: MarketSignal,
    market_context: Any | None,
) -> float:
    if market_context is None:
        return float(
            signal.score
        )

    return float(
        getattr(
            market_context,
            "adjusted_score",
            signal.score,
        )
    )


# =========================================================
# COMMANDS
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    chat = update.effective_chat
    user = update.effective_user

    if chat is not None:
        set_runtime_chat_id(
            chat.id
        )

    selected_pair = (
        get_selected_pair()
    )

    user_name = (
        user.first_name
        if user is not None
        else "Jan"
    )

    text = (
        f"✅ Jan Crypto Signal Bot is online, "
        f"{user_name}.\n\n"
        f"Selected pair: {selected_pair}\n"
        f"Automatic monitoring: "
        f"{'ON' if is_monitor_enabled() else 'OFF'}\n"
        f"Scan interval: "
        f"{MONITOR_INTERVAL_SECONDS} seconds\n\n"
        "Commands:\n"
        "/price - Current selected-pair price\n"
        "/scan - Full multi-timeframe scan\n"
        "/analysis - Same as /scan\n"
        "/timeframes - Compact timeframe view\n"
        "/pair BTCUSDT - Change selected pair\n"
        "/watch SOL ETH BTC - Replace watchlist\n"
        "/addwatch XRP - Add a pair\n"
        "/removewatch XRP - Remove a pair\n"
        "/watchlist - Show monitored pairs\n"
        "/market - BTC, dominance and VIX context\n"
        "/monitor on - Enable automatic alerts\n"
        "/monitor off - Disable automatic alerts\n"
        "/setups - Show active managed setups\n"
        "/status - Bot and monitor status"
    )

    await send_long_message(
        update,
        text,
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await start_command(
        update,
        context,
    )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    state = get_state_snapshot()

    watchlist = ", ".join(
        state["watchlist"]
    )

    text = (
        "🟢 BOT STATUS: ONLINE\n\n"
        f"Selected pair: "
        f"{state['selected_pair']}\n"
        f"Monitor: "
        f"{'ON' if state['monitor_enabled'] else 'OFF'}\n"
        f"Interval: "
        f"{MONITOR_INTERVAL_SECONDS} seconds\n"
        f"Market source: Binance\n"
        f"Timeframes: 5m, 15m, 1h, 4h, 8h, 1d\n"
        f"Macro context module: "
        f"{'AVAILABLE' if MARKET_CONTEXT_AVAILABLE else 'NOT INSTALLED'}\n"
        f"Telegram destination: "
        f"{get_destination_chat_id() or 'Not registered'}\n\n"
        f"Watchlist:\n{watchlist}\n\n"
        "Automatic trading: DISABLED\n"
        "Mode: Analysis and alerts only"
    )

    await send_long_message(
        update,
        text,
    )


async def price_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    symbol = get_selected_pair()

    waiting = await update.effective_message.reply_text(
        f"⏳ Retrieving {symbol} price..."
    )

    try:
        ticker = await fetch_ticker(
            symbol
        )

        price = float(
            ticker.get(
                "lastPrice",
                0,
            )
        )

        change = float(
            ticker.get(
                "priceChangePercent",
                0,
            )
        )

        high = float(
            ticker.get(
                "highPrice",
                0,
            )
        )

        low = float(
            ticker.get(
                "lowPrice",
                0,
            )
        )

        volume = float(
            ticker.get(
                "quoteVolume",
                0,
            )
        )

        text = (
            f"💰 {symbol}\n\n"
            f"Price: {price_text(price)}\n"
            f"24h change: {change:+.2f}%\n"
            f"24h high: {price_text(high)}\n"
            f"24h low: {price_text(low)}\n"
            f"Quote volume: "
            f"${volume:,.0f}\n\n"
            "Source: Binance Spot"
        )

        await waiting.edit_text(
            text
        )

    except Exception as error:
        logger.exception(
            "Price command failed for %s.",
            symbol,
        )

        await waiting.edit_text(
            f"⚠️ Could not retrieve {symbol} price.\n\n"
            f"Error: {type(error).__name__}: {error}"
        )


async def scan_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    symbol = get_selected_pair()

    waiting = await update.effective_message.reply_text(
        f"🔍 Scanning {symbol} across "
        "5m, 15m, 1h, 4h, 8h and daily..."
    )

    try:
        signal, macro_context, _ = (
            await analyze_symbol(
                symbol,
                include_context=True,
            )
        )

        message = build_scan_message(
            signal,
            macro_context,
        )

        await edit_or_reply(
            update,
            waiting,
            message,
        )

    except Exception as error:
        logger.exception(
            "Scan failed for %s.",
            symbol,
        )

        await waiting.edit_text(
            f"⚠️ The {symbol} scan failed.\n\n"
            f"Error: {type(error).__name__}: {error}"
        )


async def analysis_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await scan_command(
        update,
        context,
    )


async def timeframes_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    symbol = get_selected_pair()

    waiting = await update.effective_message.reply_text(
        f"⏳ Reading {symbol} timeframes..."
    )

    try:
        signal, _, _ = await analyze_symbol(
            symbol,
            include_context=False,
        )

        lines = [
            f"📊 {symbol} TIMEFRAMES",
            "",
            f"Combined score: {signal.score:+.1f}",
            f"Direction: {signal.direction}",
            "",
        ]

        for interval in [
            "5m",
            "15m",
            "1h",
            "4h",
            "8h",
            "1d",
        ]:
            analysis = signal.analyses.get(
                interval
            )

            if analysis is None:
                lines.append(
                    f"⚠️ {interval}: unavailable"
                )

                continue

            lines.append(
                f"{interval}: "
                f"{analysis.direction} "
                f"({analysis.score:+.0f}) | "
                f"RSI {analysis.rsi:.1f} | "
                f"ADX {analysis.adx:.1f} | "
                f"RVOL {analysis.relative_volume:.2f}x"
            )

        await waiting.edit_text(
            "\n".join(
                lines
            )
        )

    except Exception as error:
        logger.exception(
            "Timeframe command failed."
        )

        await waiting.edit_text(
            f"⚠️ Timeframe analysis failed.\n\n"
            f"Error: {type(error).__name__}: {error}"
        )


async def pair_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not context.args:
        await update.effective_message.reply_text(
            "Current pair: "
            f"{get_selected_pair()}\n\n"
            "Example:\n"
            "/pair BTCUSDT\n"
            "/pair ETH\n"
            "/pair XRP"
        )

        return

    raw_symbol = context.args[0]

    # bot_state normalizes ETH into ETHUSDT, etc.
    from bot_state import normalize_symbol

    try:
        symbol = normalize_symbol(
            raw_symbol
        )

        waiting = (
            await update.effective_message.reply_text(
                f"⏳ Checking {symbol} on Binance..."
            )
        )

        await validate_market_symbol(
            symbol
        )

        set_selected_pair(
            symbol
        )

        add_to_watchlist(
            symbol
        )

        await waiting.edit_text(
            f"✅ Selected pair changed to {symbol}.\n\n"
            "Use /price or /scan now."
        )

    except Exception as error:
        logger.exception(
            "Pair change failed."
        )

        await update.effective_message.reply_text(
            f"⚠️ Could not select that pair.\n\n"
            f"Error: {type(error).__name__}: {error}"
        )


async def watch_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not context.args:
        await update.effective_message.reply_text(
            "Usage:\n"
            "/watch BTC ETH SOL XRP\n\n"
            "This replaces the current watchlist."
        )

        return

    from bot_state import normalize_symbol

    try:
        symbols = [
            normalize_symbol(
                value
            )
            for value in context.args
        ]

        symbols = symbols[
            :MAX_MONITORED_PAIRS
        ]

        waiting = (
            await update.effective_message.reply_text(
                "⏳ Validating watchlist..."
            )
        )

        validations = await asyncio.gather(
            *[
                validate_market_symbol(
                    symbol
                )
                for symbol in symbols
            ],
            return_exceptions=True,
        )

        valid_symbols: list[str] = []
        invalid_symbols: list[str] = []

        for symbol, result in zip(
            symbols,
            validations,
        ):
            if isinstance(
                result,
                Exception,
            ):
                invalid_symbols.append(
                    symbol
                )
            else:
                valid_symbols.append(
                    symbol
                )

        if not valid_symbols:
            raise ValueError(
                "None of the supplied pairs were valid."
            )

        set_watchlist(
            valid_symbols
        )

        text = (
            "✅ Watchlist updated:\n"
            + "\n".join(
                f"• {symbol}"
                for symbol in valid_symbols
            )
        )

        if invalid_symbols:
            text += (
                "\n\nSkipped:\n"
                + "\n".join(
                    f"• {symbol}"
                    for symbol in invalid_symbols
                )
            )

        await waiting.edit_text(
            text
        )

    except Exception as error:
        logger.exception(
            "Watchlist update failed."
        )

        await update.effective_message.reply_text(
            f"⚠️ Watchlist update failed.\n\n"
            f"Error: {type(error).__name__}: {error}"
        )


async def addwatch_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /addwatch SUI"
        )

        return

    from bot_state import normalize_symbol

    try:
        symbol = normalize_symbol(
            context.args[0]
        )

        await validate_market_symbol(
            symbol
        )

        watchlist = add_to_watchlist(
            symbol
        )

        await update.effective_message.reply_text(
            f"✅ {symbol} added.\n\n"
            f"Watchlist: {', '.join(watchlist)}"
        )

    except Exception as error:
        await update.effective_message.reply_text(
            f"⚠️ Could not add pair.\n\n"
            f"Error: {type(error).__name__}: {error}"
        )


async def removewatch_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /removewatch DOGE"
        )

        return

    try:
        watchlist = remove_from_watchlist(
            context.args[0]
        )

        await update.effective_message.reply_text(
            "✅ Pair removed.\n\n"
            f"Watchlist: {', '.join(watchlist)}"
        )

    except Exception as error:
        await update.effective_message.reply_text(
            f"⚠️ Could not remove pair.\n\n"
            f"Error: {type(error).__name__}: {error}"
        )


async def watchlist_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    watchlist = get_watchlist()

    text = (
        "👁 CURRENT WATCHLIST\n\n"
        + "\n".join(
            f"{index}. {symbol}"
            for index, symbol in enumerate(
                watchlist,
                start=1,
            )
        )
        + f"\n\nSelected: {get_selected_pair()}"
    )

    await update.effective_message.reply_text(
        text
    )


async def monitor_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not context.args:
        await update.effective_message.reply_text(
            "Monitor is currently "
            f"{'ON' if is_monitor_enabled() else 'OFF'}.\n\n"
            "Use:\n"
            "/monitor on\n"
            "/monitor off"
        )

        return

    choice = context.args[0].lower()

    if choice in {
        "on",
        "start",
        "enable",
    }:
        set_monitor_enabled(
            True
        )

        if update.effective_chat is not None:
            set_runtime_chat_id(
                update.effective_chat.id
            )

        await update.effective_message.reply_text(
            "✅ Automatic monitoring enabled."
        )

        return

    if choice in {
        "off",
        "stop",
        "disable",
    }:
        set_monitor_enabled(
            False
        )

        await update.effective_message.reply_text(
            "⏸ Automatic monitoring disabled."
        )

        return

    await update.effective_message.reply_text(
        "Use /monitor on or /monitor off."
    )


async def setups_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await send_long_message(
        update,
        build_active_setups_message(),
    )


async def market_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    symbol = get_selected_pair()

    waiting = await update.effective_message.reply_text(
        "🌍 Loading global market context..."
    )

    if not MARKET_CONTEXT_AVAILABLE:
        await waiting.edit_text(
            "⚠️ market_context.py is not installed yet.\n\n"
            "Technical scans still work normally."
        )

        return

    try:
        signal, macro_context, _ = (
            await analyze_symbol(
                symbol,
                include_context=True,
            )
        )

        if macro_context is None:
            raise RuntimeError(
                "The macro provider returned no context."
            )

        lines = [
            "🌍 GLOBAL MARKET CONTEXT",
            "",
            f"Selected pair: {symbol}",
            f"Technical score: {signal.score:+.1f}",
            (
                "Adjusted score: "
                f"{macro_context.adjusted_score:+.1f}"
            ),
            (
            "Macro adjustment: "
            f"{macro_context.score_adjustment:+.1f}"
            ),
            (
            "Macro bias: "
            f"{macro_context.macro_bias}"
            ),
            (
            "Macro score: "
            f"{macro_context.macro_score:+.1f}"
            ),
            "",
            (
                f"BTC: {macro_context.btc_direction} "
                f"({macro_context.btc_score:+.1f})"
            ),
            (
                f"ETH: {macro_context.eth_direction} "
                f"({macro_context.eth_score:+.1f})"
            ),
            (
                "Fear & Greed: "
                f"{getattr(macro_context, 'fear_greed_value', 50):.0f} "
                f"({getattr(macro_context, 'fear_greed_label', getattr(macro_context, 'fear_greed_classification', 'NEUTRAL'))}) "
                f"[{getattr(macro_context, 'fear_greed_change', 0.0):+.0f}]"
            ),
            (
                "BTC correlation: "
                f"{macro_context.btc_correlation:.2f} "
                f"({macro_context.correlation_strength})"
            ),
            (
                "BTC dominance: "
                f"{macro_context.btc_dominance:.2f}% "
                f"({macro_context.btc_dominance_effect})"
            ),
            (
                "Crypto market 24h: "
                f"{macro_context.crypto_market_change_24h:+.2f}%"
            ),
            (
                f"VIX: {macro_context.vix_value:.2f} "
                f"({macro_context.vix_regime})"
            ),
            (
                f"Funding: {macro_context.funding_rate * 100:+.4f}% "
                f"({macro_context.funding_label}, {macro_context.derivatives_provider})"
            ),
            (
                f"Open interest: ${macro_context.open_interest_value:,.0f}"
            ),
            (
                "OI change: "
                f"{macro_context.open_interest_change_5m:+.2f}% (5m), "
                f"{macro_context.open_interest_change_1h:+.2f}% (1h)"
            ),
            ]    
        if macro_context.macro_reasons:
            lines.extend([
                "",
                "MACRO BIAS FACTORS",
                *[
                    f"• {reason}"
                    for reason in macro_context.macro_reasons[:6]
                ],
            ])

        if macro_context.reasons:
            lines.extend([
                "",
                "SUPPORTING FACTORS",
                *[
                    f"• {reason}"
                    for reason in macro_context.reasons[:6]
                ],
            ])

        if macro_context.warnings:
            lines.extend([
                "",
                "RISKS",
                *[
                    f"• {warning}"
                    for warning in macro_context.warnings[:6]
                ],
            ])

        await edit_or_reply(
            update,
            waiting,
            "\n".join(lines),
        )
    except Exception as error:
        logger.exception(
            "Market-context command failed."
        )

        await waiting.edit_text(
            "⚠️ Global market context failed.\n\n"
            f"Error: {type(error).__name__}: {error}"
        )


# =========================================================
# AUTOMATIC WATCHLIST MONITOR
# =========================================================

async def monitor_one_symbol(
    application: Application,
    symbol: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, float] | None:
    async with semaphore:
        try:
            # Technical scan first. Macro data are only loaded
            # when a setup is strong enough to matter.
            signal, _, _ = await analyze_symbol(
                symbol,
                include_context=False,
            )

            macro_context = None

            if abs(
                signal.score
            ) >= 50:
                try:
                    _, macro_context, _ = (
                        await analyze_symbol(
                            symbol,
                            include_context=True,
                        )
                    )

                except Exception:
                    logger.exception(
                        "Context enrichment failed "
                        "for %s.",
                        symbol,
                    )

            decision = evaluate_signal_alert(
                signal,
                macro_context,
            )

            derivatives_data = None
            try:
                derivatives_data = await fetch_derivatives_context(symbol)
            except Exception:
                logger.exception("Derivatives monitoring failed for %s.", symbol)

            derivatives_decision = evaluate_derivatives_alert(
                signal,
                derivatives_data,
            )

            for alert_decision in (decision, derivatives_decision):
                if not alert_decision.should_send:
                    continue
                destination = (
                    get_destination_chat_id()
                )

                if destination:
                    await application.bot.send_message(
                        chat_id=destination,
                        text=alert_decision.message[
                            :TELEGRAM_MESSAGE_LIMIT
                        ],
                    )

                    logger.info(
                        "Sent %s alert for %s: %s",
                        alert_decision.alert_type,
                        symbol,
                        alert_decision.reason,
                    )

                else:
                    logger.warning(
                        "Alert generated for %s, "
                        "but no Telegram chat ID "
                        "is registered.",
                        symbol,
                    )

            return (
                symbol,
                adjusted_score(
                    signal,
                    macro_context,
                ),
            )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Automatic scan failed for %s.",
                symbol,
            )

            return None


async def monitor_loop(
    application: Application,
) -> None:
    await asyncio.sleep(
        INITIAL_MONITOR_DELAY_SECONDS
    )

    semaphore = asyncio.Semaphore(
        max(
            1,
            SCAN_CONCURRENCY,
        )
    )

    logger.info(
        "Automatic market monitor started."
    )

    while True:
        try:
            if is_monitor_enabled():
                watchlist = get_watchlist()[
                    :MAX_MONITORED_PAIRS
                ]

                tasks = [
                    monitor_one_symbol(
                        application,
                        symbol,
                        semaphore,
                    )
                    for symbol in watchlist
                ]

                results = await asyncio.gather(
                    *tasks,
                    return_exceptions=True,
                )

                ranked = [
                    result
                    for result in results
                    if (
                        isinstance(
                            result,
                            tuple,
                        )
                        and len(result) == 2
                    )
                ]

                ranked.sort(
                    key=lambda item: abs(
                        item[1]
                    ),
                    reverse=True,
                )

                if ranked:
                    logger.info(
                        "Scan cycle completed. "
                        "Top setup: %s %.1f",
                        ranked[0][0],
                        ranked[0][1],
                    )

            await asyncio.sleep(
                max(
                    15,
                    MONITOR_INTERVAL_SECONDS,
                )
            )

        except asyncio.CancelledError:
            logger.info(
                "Automatic monitor stopped."
            )

            raise

        except Exception:
            logger.exception(
                "Monitor loop encountered an error."
            )

            await asyncio.sleep(
                15
            )


# =========================================================
# APPLICATION LIFECYCLE
# =========================================================

async def post_init(
    application: Application,
) -> None:
    commands = [
        BotCommand(
            "start",
            "Start and register the bot",
        ),
        BotCommand(
            "price",
            "Current selected-pair price",
        ),
        BotCommand(
            "scan",
            "Run full market analysis",
        ),
        BotCommand(
            "analysis",
            "Run full market analysis",
        ),
        BotCommand(
            "timeframes",
            "Show all timeframe scores",
        ),
        BotCommand(
            "pair",
            "Change selected pair",
        ),
        BotCommand(
            "watch",
            "Replace watchlist",
        ),
        BotCommand(
            "watchlist",
            "Show watchlist",
        ),
        BotCommand(
            "market",
            "Show macro market context",
        ),
        BotCommand(
            "monitor",
            "Turn automatic alerts on or off",
        ),
        BotCommand(
            "setups",
            "Show active managed setups",
        ),
        BotCommand(
            "status",
            "Show bot status",
        ),
    ]

    await application.bot.set_my_commands(
        commands
    )

    monitor_task = asyncio.create_task(
        monitor_loop(
            application
        ),
        name="market-monitor",
    )

    application.bot_data[
        "monitor_task"
    ] = monitor_task

    logger.info(
        "Jan Crypto Signal Bot initialized."
    )


async def post_shutdown(
    application: Application,
) -> None:
    monitor_task = application.bot_data.get(
        "monitor_task"
    )

    if monitor_task is not None:
        monitor_task.cancel()

        with suppress(
            asyncio.CancelledError
        ):
            await monitor_task


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.error(
        "Telegram update caused an error.",
        exc_info=context.error,
    )


# =========================================================
# START BOT
# =========================================================

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN is missing "
            "from Railway Variables."
        )

    if "\n" in TELEGRAM_TOKEN:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN still contains "
            "a newline."
        )

    if ":" not in TELEGRAM_TOKEN:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN does not look "
            "like a valid Telegram bot token."
        )

    application = (
        ApplicationBuilder()
        .token(
            TELEGRAM_TOKEN
        )
        .post_init(
            post_init
        )
        .post_shutdown(
            post_shutdown
        )
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "price",
            price_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "scan",
            scan_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "analysis",
            analysis_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "timeframes",
            timeframes_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "pair",
            pair_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "watch",
            watch_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "addwatch",
            addwatch_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "removewatch",
            removewatch_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "watchlist",
            watchlist_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "monitor",
            monitor_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "market",
            market_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "setups",
            setups_command,
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Jan Crypto Signal Bot starting..."
    )

    application.run_polling(
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
