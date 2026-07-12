import asyncio
import logging
import os
from typing import Any

import aiohttp
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# RAILWAY VARIABLES
# =========================================================

TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "",
).strip()

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "",
).strip()


# =========================================================
# BINANCE SETTINGS
# =========================================================

BINANCE_BASE_URL = "https://data-api.binance.vision"
SYMBOL = "SOLUSDT"

TIMEFRAMES = {
    "5m": "5 minutes",
    "15m": "15 minutes",
    "1h": "1 hour",
    "4h": "4 hours",
    "8h": "8 hours",
    "1d": "1 day",
}


# =========================================================
# BINANCE REQUESTS
# =========================================================

async def binance_request(
    endpoint: str,
    params: dict[str, Any],
) -> Any:
    url = f"{BINANCE_BASE_URL}{endpoint}"

    timeout = aiohttp.ClientTimeout(
        total=25,
        connect=10,
        sock_read=20,
    )

    headers = {
        "Accept": "application/json",
        "User-Agent": "JanSOLSignalBot/1.0",
    }

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
    ) as session:
        async with session.get(
            url,
            params=params,
        ) as response:
            response_text = await response.text()

            if response.status != 200:
                raise RuntimeError(
                    f"Binance HTTP {response.status}: "
                    f"{response_text[:300]}"
                )

            try:
                return await response.json()
            except Exception as error:
                raise RuntimeError(
                    f"Invalid Binance response: "
                    f"{response_text[:300]}"
                ) from error


async def get_ticker_24h() -> dict[str, Any]:
    data = await binance_request(
        "/api/v3/ticker/24hr",
        {
            "symbol": SYMBOL,
        },
    )

    if not isinstance(data, dict):
        raise ValueError(
            f"Unexpected ticker response: {data}"
        )

    return data


async def get_klines(
    interval: str,
    limit: int = 250,
) -> list[dict[str, float | int]]:
    raw_candles = await binance_request(
        "/api/v3/klines",
        {
            "symbol": SYMBOL,
            "interval": interval,
            "limit": limit,
        },
    )

    if not isinstance(raw_candles, list):
        raise ValueError(
            f"Unexpected candle response for {interval}: "
            f"{raw_candles}"
        )

    candles: list[dict[str, float | int]] = []

    for raw_candle in raw_candles:
        if not isinstance(raw_candle, list):
            continue

        if len(raw_candle) < 7:
            continue

        try:
            candle = {
                "open_time": int(raw_candle[0]),
                "open": float(raw_candle[1]),
                "high": float(raw_candle[2]),
                "low": float(raw_candle[3]),
                "close": float(raw_candle[4]),
                "volume": float(raw_candle[5]),
                "close_time": int(raw_candle[6]),
            }

            candles.append(candle)

        except (TypeError, ValueError):
            continue

    if len(candles) < 30:
        raise ValueError(
            f"Only {len(candles)} usable candles received "
            f"for {interval}."
        )

    return candles


# =========================================================
# INDICATOR CALCULATIONS
# =========================================================

def calculate_ema(
    values: list[float],
    period: int,
) -> float | None:
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    ema = sum(values[:period]) / period

    for value in values[period:]:
        ema = (
            value * multiplier
            + ema * (1 - multiplier)
        )

    return ema


def calculate_rsi(
    values: list[float],
    period: int = 14,
) -> float | None:
    if len(values) <= period:
        return None

    changes = [
        values[index] - values[index - 1]
        for index in range(1, len(values))
    ]

    gains = [
        max(change, 0)
        for change in changes
    ]

    losses = [
        abs(min(change, 0))
        for change in changes
    ]

    average_gain = (
        sum(gains[:period]) / period
    )

    average_loss = (
        sum(losses[:period]) / period
    )

    for index in range(
        period,
        len(changes),
    ):
        average_gain = (
            average_gain * (period - 1)
            + gains[index]
        ) / period

        average_loss = (
            average_loss * (period - 1)
            + losses[index]
        ) / period

    if average_loss == 0:
        return 100.0

    relative_strength = (
        average_gain / average_loss
    )

    return 100 - (
        100 / (1 + relative_strength)
    )


def determine_trend(
    current_price: float,
    ema20: float | None,
    ema50: float | None,
    ema200: float | None,
    rsi: float | None,
) -> tuple[str, str]:
    bullish_points = 0
    bearish_points = 0

    if ema20 is not None:
        if current_price > ema20:
            bullish_points += 1
        else:
            bearish_points += 1

    if ema50 is not None:
        if current_price > ema50:
            bullish_points += 1
        else:
            bearish_points += 1

    if ema200 is not None:
        if current_price > ema200:
            bullish_points += 1
        else:
            bearish_points += 1

    if (
        ema20 is not None
        and ema50 is not None
    ):
        if ema20 > ema50:
            bullish_points += 1
        else:
            bearish_points += 1

    if rsi is not None:
        if rsi >= 55:
            bullish_points += 1
        elif rsi <= 45:
            bearish_points += 1

    if bullish_points >= bearish_points + 2:
        return "BULLISH", "🟢"

    if bearish_points >= bullish_points + 2:
        return "BEARISH", "🔴"

    return "NEUTRAL", "🟡"


def format_number(
    value: float | None,
    decimals: int = 2,
) -> str:
    if value is None:
        return "N/A"

    return f"{value:,.{decimals}f}"


def analyze_candles(
    candles: list[dict[str, float | int]],
) -> dict[str, Any]:
    closes = [
        float(candle["close"])
        for candle in candles
    ]

    highs = [
        float(candle["high"])
        for candle in candles
    ]

    lows = [
        float(candle["low"])
        for candle in candles
    ]

    current_price = closes[-1]

    ema20 = calculate_ema(
        closes,
        20,
    )

    ema50 = calculate_ema(
        closes,
        50,
    )

    ema200 = calculate_ema(
        closes,
        200,
    )

    rsi = calculate_rsi(
        closes,
        14,
    )

    trend, trend_emoji = determine_trend(
        current_price=current_price,
        ema20=ema20,
        ema50=ema50,
        ema200=ema200,
        rsi=rsi,
    )

    recent_highs = highs[-50:]
    recent_lows = lows[-50:]

    resistance = max(recent_highs)
    support = min(recent_lows)

    return {
        "price": current_price,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "rsi": rsi,
        "trend": trend,
        "trend_emoji": trend_emoji,
        "support": support,
        "resistance": resistance,
    }


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    user_id = (
        update.effective_user.id
        if update.effective_user
        else "Unknown"
    )

    await update.message.reply_text(
        "✅ Jan SOL Signal Bot is online!\n\n"
        f"Your Telegram ID: {user_id}\n\n"
        "Available commands:\n"
        "/price - Live SOL price and 24-hour data\n"
        "/levels - Support, resistance and EMAs\n"
        "/timeframes - Check all six timeframes\n"
        "/status - Bot status"
    )


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "🟢 Bot status: ONLINE\n"
        "📈 Market: SOL/USDT\n"
        "🏦 Exchange: Binance\n"
        "🕯 Timeframes: 5m, 15m, 1h, 4h, 8h, 1d\n"
        "🤖 Mode: Alerts only\n"
        "🚫 Automatic trading: Disabled"
    )


async def price(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    loading_message = (
        await update.message.reply_text(
            "⏳ Retrieving SOL price from Binance..."
        )
    )

    try:
        ticker = await get_ticker_24h()

        current_price = float(
            ticker["lastPrice"]
        )

        price_change_percent = float(
            ticker["priceChangePercent"]
        )

        high_price = float(
            ticker["highPrice"]
        )

        low_price = float(
            ticker["lowPrice"]
        )

        quote_volume = float(
            ticker["quoteVolume"]
        )

        change_emoji = (
            "🟢"
            if price_change_percent >= 0
            else "🔴"
        )

        await loading_message.edit_text(
            "💰 SOL/USDT LIVE PRICE\n\n"
            f"Price: ${current_price:,.2f}\n"
            f"{change_emoji} 24h change: "
            f"{price_change_percent:+.2f}%\n"
            f"⬆️ 24h high: ${high_price:,.2f}\n"
            f"⬇️ 24h low: ${low_price:,.2f}\n"
            f"💵 24h volume: ${quote_volume:,.0f}\n\n"
            "Source: Binance\n"
            "Public market data"
        )

    except Exception as error:
        logger.exception(
            "Price request failed: %s",
            error,
        )

        await loading_message.edit_text(
            "⚠️ I could not retrieve the SOL price.\n\n"
            f"Error: {type(error).__name__}\n"
            "Check the Railway deploy log."
        )


async def levels(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    loading_message = (
        await update.message.reply_text(
            "⏳ Calculating SOL support and resistance..."
        )
    )

    try:
        candles = await get_klines(
            interval="15m",
            limit=250,
        )

        result = analyze_candles(
            candles
        )

        price_value = float(
            result["price"]
        )

        resistance = float(
            result["resistance"]
        )

        support = float(
            result["support"]
        )

        resistance_distance = (
            (resistance - price_value)
            / price_value
            * 100
        )

        support_distance = (
            (support - price_value)
            / price_value
            * 100
        )

        await loading_message.edit_text(
            "📊 SOL/USDT LEVELS\n\n"
            f"💰 Price: ${price_value:,.2f}\n\n"
            f"🔺 Resistance: ${resistance:,.2f}\n"
            f"Distance: {resistance_distance:+.2f}%\n\n"
            f"🔻 Support: ${support:,.2f}\n"
            f"Distance: {support_distance:+.2f}%\n\n"
            f"EMA 20: ${format_number(result['ema20'])}\n"
            f"EMA 50: ${format_number(result['ema50'])}\n"
            f"EMA 200: ${format_number(result['ema200'])}\n"
            f"RSI 14: {format_number(result['rsi'])}\n\n"
            "Calculated from Binance 15-minute candles."
        )

    except Exception as error:
        logger.exception(
            "Levels request failed: %s",
            error,
        )

        await loading_message.edit_text(
            "⚠️ SOL levels could not be calculated.\n\n"
            f"Error: {type(error).__name__}: {error}"
        )


async def timeframes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    loading_message = (
        await update.message.reply_text(
            "⏳ Loading six Binance timeframes..."
        )
    )

    report_lines = [
        "📊 SOL/USDT MULTI-TIMEFRAME CHECK",
        "",
    ]

    loaded_count = 0
    failed_count = 0

    for interval, display_name in TIMEFRAMES.items():
        try:
            candles = await get_klines(
                interval=interval,
                limit=250,
            )

            result = analyze_candles(
                candles
            )

            trend = result["trend"]
            trend_emoji = result["trend_emoji"]
            current_price = float(
                result["price"]
            )

            rsi_text = format_number(
                result["rsi"],
                1,
            )

            report_lines.append(
                f"{trend_emoji} {display_name}: {trend}"
            )

            report_lines.append(
                f"Price ${current_price:,.2f} | RSI {rsi_text}"
            )

            report_lines.append("")

            loaded_count += 1

        except Exception as error:
            logger.exception(
                "Timeframe %s failed: %s",
                interval,
                error,
            )

            report_lines.append(
                f"⚠️ {display_name}: Request failed"
            )

            report_lines.append(
                f"{type(error).__name__}: "
                f"{str(error)[:100]}"
            )

            report_lines.append("")

            failed_count += 1

        await asyncio.sleep(0.35)

    report_lines.extend(
        [
            f"✅ Loaded: {loaded_count}",
            f"⚠️ Failed: {failed_count}",
            "Source: Binance spot candles",
        ]
    )

    await loading_message.edit_text(
        "\n".join(report_lines)
    )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.error(
        "Telegram update caused an error",
        exc_info=context.error,
    )


# =========================================================
# BOT STARTUP
# =========================================================

async def post_init(
    application: Application,
) -> None:
    await application.bot.delete_webhook(
        drop_pending_updates=True
    )

    logger.info(
        "Webhook cleared and bot initialized"
    )


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN is missing "
            "from Railway Variables."
        )

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status,
        )
    )

    application.add_handler(
        CommandHandler(
            "price",
            price,
        )
    )

    application.add_handler(
        CommandHandler(
            "levels",
            levels,
        )
    )

    application.add_handler(
        CommandHandler(
            "timeframes",
            timeframes,
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Jan SOL Signal Bot started"
    )

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
