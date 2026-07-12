import asyncio
import logging
import os
from typing import Any

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ---------------------------------------------------------
# LOGGING
# ---------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

BINANCE_BASE_URL = "https://data-api.binance.vision"

SYMBOL = "SOLUSDT"
TIMEFRAME = "15m"
CANDLE_LIMIT = 250

HTTP_TIMEOUT = 20.0


# ---------------------------------------------------------
# BINANCE DATA
# ---------------------------------------------------------

async def binance_request(
    endpoint: str,
    params: dict[str, Any],
) -> Any:
    url = f"{BINANCE_BASE_URL}{endpoint}"

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


async def get_current_price() -> float:
    data = await binance_request(
        "/api/v3/ticker/price",
        {"symbol": SYMBOL},
    )

    return float(data["price"])


async def get_24h_ticker() -> dict[str, Any]:
    return await binance_request(
        "/api/v3/ticker/24hr",
        {"symbol": SYMBOL},
    )


async def get_klines(
    interval: str = TIMEFRAME,
    limit: int = CANDLE_LIMIT,
) -> list[list[Any]]:
    data = await binance_request(
        "/api/v3/klines",
        {
            "symbol": SYMBOL,
            "interval": interval,
            "limit": limit,
        },
    )

    if not isinstance(data, list) or len(data) < 50:
        raise ValueError("Binance returned insufficient candle data.")

    return data


# ---------------------------------------------------------
# INDICATOR CALCULATIONS
# ---------------------------------------------------------

def calculate_ema(
    values: list[float],
    period: int,
) -> float:
    if len(values) < period:
        raise ValueError(
            f"Not enough values to calculate EMA {period}."
        )

    multiplier = 2 / (period + 1)

    ema_value = sum(values[:period]) / period

    for value in values[period:]:
        ema_value = (
            value * multiplier
            + ema_value * (1 - multiplier)
        )

    return ema_value


def calculate_ema_series(
    values: list[float],
    period: int,
) -> list[float]:
    if len(values) < period:
        raise ValueError(
            f"Not enough values to calculate EMA series {period}."
        )

    multiplier = 2 / (period + 1)
    ema_value = sum(values[:period]) / period

    result = [ema_value]

    for value in values[period:]:
        ema_value = (
            value * multiplier
            + ema_value * (1 - multiplier)
        )
        result.append(ema_value)

    return result


def calculate_rsi(
    values: list[float],
    period: int = 14,
) -> float:
    if len(values) <= period:
        raise ValueError("Not enough values to calculate RSI.")

    changes = [
        values[index] - values[index - 1]
        for index in range(1, len(values))
    ]

    gains = [
        max(change, 0.0)
        for change in changes
    ]

    losses = [
        abs(min(change, 0.0))
        for change in changes
    ]

    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    for index in range(period, len(changes)):
        average_gain = (
            (average_gain * (period - 1)) + gains[index]
        ) / period

        average_loss = (
            (average_loss * (period - 1)) + losses[index]
        ) / period

    if average_loss == 0:
        return 100.0

    relative_strength = average_gain / average_loss

    return 100 - (100 / (1 + relative_strength))


def calculate_macd(
    values: list[float],
) -> tuple[float, float, float]:
    if len(values) < 50:
        raise ValueError("Not enough values to calculate MACD.")

    ema12_series = calculate_ema_series(values, 12)
    ema26_series = calculate_ema_series(values, 26)

    offset = len(ema12_series) - len(ema26_series)

    aligned_ema12 = ema12_series[offset:]

    macd_series = [
        ema12 - ema26
        for ema12, ema26 in zip(
            aligned_ema12,
            ema26_series,
        )
    ]

    if len(macd_series) < 9:
        raise ValueError(
            "Not enough MACD values for signal line."
        )

    signal_line = calculate_ema(macd_series, 9)
    macd_line = macd_series[-1]
    histogram = macd_line - signal_line

    return macd_line, signal_line, histogram


def percent_difference(
    first: float,
    second: float,
) -> float:
    if second == 0:
        return 0.0

    return ((first - second) / second) * 100


# ---------------------------------------------------------
# MARKET ANALYSIS
# ---------------------------------------------------------

async def build_market_analysis() -> dict[str, Any]:
    klines = await get_klines()

    closes = [
        float(candle[4])
        for candle in klines
    ]

    highs = [
        float(candle[2])
        for candle in klines
    ]

    lows = [
        float(candle[3])
        for candle in klines
    ]

    volumes = [
        float(candle[5])
        for candle in klines
    ]

    current_price = closes[-1]

    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    ema200 = calculate_ema(closes, 200)

    rsi = calculate_rsi(closes, 14)

    macd_line, macd_signal, macd_histogram = calculate_macd(
        closes
    )

    recent_high = max(highs[-20:])
    recent_low = min(lows[-20:])

    previous_high = max(highs[-40:-20])
    previous_low = min(lows[-40:-20])

    average_volume = sum(volumes[-21:-1]) / 20
    latest_volume = volumes[-1]

    volume_ratio = (
        latest_volume / average_volume
        if average_volume > 0
        else 0.0
    )

    score = 0
    reasons: list[str] = []

    if current_price > ema20:
        score += 1
        reasons.append("Price is above EMA 20")
    else:
        score -= 1
        reasons.append("Price is below EMA 20")

    if ema20 > ema50:
        score += 1
        reasons.append("EMA 20 is above EMA 50")
    else:
        score -= 1
        reasons.append("EMA 20 is below EMA 50")

    if ema50 > ema200:
        score += 2
        reasons.append("EMA 50 is above EMA 200")
    else:
        score -= 2
        reasons.append("EMA 50 is below EMA 200")

    if macd_line > macd_signal:
        score += 1
        reasons.append("MACD is above its signal line")
    else:
        score -= 1
        reasons.append("MACD is below its signal line")

    if macd_histogram > 0:
        score += 1
        reasons.append("MACD histogram is positive")
    else:
        score -= 1
        reasons.append("MACD histogram is negative")

    if 50 <= rsi <= 68:
        score += 1
        reasons.append("RSI supports bullish momentum")
    elif 32 <= rsi < 50:
        score -= 1
        reasons.append("RSI shows weak momentum")
    elif rsi > 72:
        score -= 1
        reasons.append("RSI may be overbought")
    elif rsi < 28:
        score += 1
        reasons.append("RSI may be oversold")

    if recent_high > previous_high:
        score += 1
        reasons.append("Recent structure made a higher high")
    else:
        score -= 1
        reasons.append("Recent structure did not make a higher high")

    if recent_low > previous_low:
        score += 1
        reasons.append("Recent structure made a higher low")
    else:
        score -= 1
        reasons.append("Recent structure made a lower low")

    if volume_ratio >= 1.5:
        score += 1
        reasons.append("Volume is significantly above average")

    if score >= 6:
        signal = "STRONG BUY"
        signal_emoji = "🚀"
    elif score >= 3:
        signal = "BUY"
        signal_emoji = "🟢"
    elif score <= -6:
        signal = "STRONG SELL"
        signal_emoji = "🔻"
    elif score <= -3:
        signal = "SELL"
        signal_emoji = "🔴"
    else:
        signal = "NEUTRAL"
        signal_emoji = "🟡"

    return {
        "price": current_price,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "rsi": rsi,
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_histogram": macd_histogram,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "volume_ratio": volume_ratio,
        "score": score,
        "signal": signal,
        "signal_emoji": signal_emoji,
        "reasons": reasons,
        "distance_ema20": percent_difference(
            current_price,
            ema20,
        ),
        "distance_ema50": percent_difference(
            current_price,
            ema50,
        ),
        "distance_ema200": percent_difference(
            current_price,
            ema200,
        ),
    }


# ---------------------------------------------------------
# TELEGRAM COMMANDS
# ---------------------------------------------------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    user_id = user.id if user else "Unknown"

    await update.effective_message.reply_text(
        "✅ Jan SOL Signal Bot is online!\n\n"
        f"Your Telegram ID: {user_id}\n\n"
        "Available commands:\n"
        "/price - Current Binance SOL price\n"
        "/analysis - Full 15-minute analysis\n"
        "/levels - Support and resistance levels\n"
        "/status - Bot status\n"
        "/help - Command list"
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.effective_message.reply_text(
        "🤖 JAN SOL SIGNAL BOT\n\n"
        "/price\n"
        "Shows the live Binance SOL/USDT price and "
        "24-hour market statistics.\n\n"
        "/analysis\n"
        "Calculates RSI, EMA 20, EMA 50, EMA 200, "
        "MACD, volume and market structure.\n\n"
        "/levels\n"
        "Shows nearby support and resistance levels.\n\n"
        "/status\n"
        "Shows whether the bot is online."
    )


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.effective_message.reply_text(
        "🟢 Bot status: ONLINE\n"
        "📈 Market: SOL/USDT\n"
        "🏦 Exchange data: Binance\n"
        "🕯 Timeframe: 15 minutes\n"
        "📡 Source: Binance public market API\n"
        "🤖 Mode: Analysis and alerts\n"
        "🚫 Automatic trading: Disabled"
    )


async def price(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = await update.effective_message.reply_text(
        "⏳ Retrieving Binance SOL market data..."
    )

    try:
        current_price, ticker = await asyncio.gather(
            get_current_price(),
            get_24h_ticker(),
        )

        price_change_percent = float(
            ticker["priceChangePercent"]
        )

        high_price = float(ticker["highPrice"])
        low_price = float(ticker["lowPrice"])
        quote_volume = float(ticker["quoteVolume"])

        direction_emoji = (
            "🟢"
            if price_change_percent >= 0
            else "🔴"
        )

        await message.edit_text(
            "💰 SOL/USDT LIVE PRICE\n\n"
            f"Price: ${current_price:,.2f}\n"
            f"{direction_emoji} 24h change: "
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

        await message.edit_text(
            "⚠️ Binance price data could not be retrieved.\n\n"
            f"Error: {type(error).__name__}\n"
            "Please try again shortly."
        )


async def analysis_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = await update.effective_message.reply_text(
        "🔍 Calculating Binance SOL analysis..."
    )

    try:
        data = await build_market_analysis()

        reasons_text = "\n".join(
            f"• {reason}"
            for reason in data["reasons"][:6]
        )

        await message.edit_text(
            "📊 SOL/USDT PREMIUM ANALYSIS\n\n"
            f"{data['signal_emoji']} Signal: "
            f"{data['signal']}\n"
            f"🎯 Signal score: {data['score']:+d}\n"
            f"💰 Price: ${data['price']:,.2f}\n"
            "🕯 Timeframe: 15 minutes\n\n"
            "MOMENTUM\n"
            f"RSI 14: {data['rsi']:.2f}\n"
            f"MACD: {data['macd']:.4f}\n"
            f"MACD signal: "
            f"{data['macd_signal']:.4f}\n"
            f"MACD histogram: "
            f"{data['macd_histogram']:.4f}\n\n"
            "TREND\n"
            f"EMA 20: ${data['ema20']:,.2f} "
            f"({data['distance_ema20']:+.2f}%)\n"
            f"EMA 50: ${data['ema50']:,.2f} "
            f"({data['distance_ema50']:+.2f}%)\n"
            f"EMA 200: ${data['ema200']:,.2f} "
            f"({data['distance_ema200']:+.2f}%)\n\n"
            "MARKET STRUCTURE\n"
            f"Resistance: ${data['recent_high']:,.2f}\n"
            f"Support: ${data['recent_low']:,.2f}\n"
            f"Volume ratio: {data['volume_ratio']:.2f}x\n\n"
            "WHY\n"
            f"{reasons_text}\n\n"
            "Source: Binance market data\n"
            "⚠️ Analysis only, not financial advice."
        )

    except Exception as error:
        logger.exception(
            "Analysis request failed: %s",
            error,
        )

        await message.edit_text(
            "⚠️ SOL analysis could not be calculated.\n\n"
            f"Error: {type(error).__name__}\n"
            "Please try again shortly."
        )


async def levels(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = await update.effective_message.reply_text(
        "📐 Calculating SOL support and resistance..."
    )

    try:
        data = await build_market_analysis()

        current_price = data["price"]
        resistance = data["recent_high"]
        support = data["recent_low"]

        upside = percent_difference(
            resistance,
            current_price,
        )

        downside = percent_difference(
            support,
            current_price,
        )

        await message.edit_text(
            "📐 SOL/USDT LEVELS\n\n"
            f"Current price: ${current_price:,.2f}\n\n"
            f"🔺 Resistance: ${resistance:,.2f}\n"
            f"Distance: {upside:+.2f}%\n\n"
            f"🔻 Support: ${support:,.2f}\n"
            f"Distance: {downside:+.2f}%\n\n"
            f"EMA 20: ${data['ema20']:,.2f}\n"
            f"EMA 50: ${data['ema50']:,.2f}\n"
            f"EMA 200: ${data['ema200']:,.2f}\n\n"
            "Calculated from Binance 15-minute candles."
        )

    except Exception as error:
        logger.exception(
            "Levels request failed: %s",
            error,
        )

        await message.edit_text(
            "⚠️ Support and resistance could not "
            "be calculated.\n\n"
            f"Error: {type(error).__name__}"
        )


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.error(
        "Telegram update caused an error",
        exc_info=context.error,
    )


# ---------------------------------------------------------
# BOT STARTUP
# ---------------------------------------------------------

async def post_init(
    application: Application,
) -> None:
    await application.bot.delete_webhook(
        drop_pending_updates=True
    )

    logger.info(
        "Telegram webhook removed and polling prepared."
    )


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN is missing from "
            "Railway Variables."
        )

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("status", status)
    )

    application.add_handler(
        CommandHandler("price", price)
    )

    application.add_handler(
        CommandHandler("analysis", analysis_command)
    )

    application.add_handler(
        CommandHandler("levels", levels)
    )

    application.add_error_handler(error_handler)

    logger.info("Jan SOL Signal Bot started")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
