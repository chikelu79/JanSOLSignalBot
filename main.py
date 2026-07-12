import asyncio
import logging
import os
from typing import Any

import aiohttp
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

BINANCE_BASE_URLS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]

SYMBOL = "SOLUSDT"

TIMEFRAMES = {
    "5m": "5 minutes",
    "15m": "15 minutes",
    "1h": "1 hour",
    "4h": "4 hours",
    "8h": "8 hours",
    "1d": "1 day",
}


async def request_binance(
    endpoint: str,
    params: dict[str, Any],
) -> Any:
    timeout = aiohttp.ClientTimeout(total=20)

    last_error: Exception | None = None

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for base_url in BINANCE_BASE_URLS:
            url = f"{base_url}{endpoint}"

            try:
                async with session.get(
                    url,
                    params=params,
                ) as response:
                    response.raise_for_status()
                    return await response.json()

            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
            ) as error:
                last_error = error

                logger.warning(
                    "Binance request failed using %s: %s",
                    base_url,
                    error,
                )

    raise RuntimeError(
        f"All Binance endpoints failed. Last error: {last_error}"
    )


async def get_ticker_data() -> dict[str, Any]:
    data = await request_binance(
        endpoint="/api/v3/ticker/24hr",
        params={
            "symbol": SYMBOL,
        },
    )

    return data


async def get_klines(
    interval: str,
    limit: int = 250,
) -> list[dict[str, float | int]]:
    raw_klines = await request_binance(
        endpoint="/api/v3/klines",
        params={
            "symbol": SYMBOL,
            "interval": interval,
            "limit": limit,
        },
    )

    candles: list[dict[str, float | int]] = []

    for item in raw_klines:
        candle = {
            "open_time": int(item[0]),
            "open": float(item[1]),
            "high": float(item[2]),
            "low": float(item[3]),
            "close": float(item[4]),
            "volume": float(item[5]),
            "close_time": int(item[6]),
            "quote_volume": float(item[7]),
            "trade_count": int(item[8]),
            "taker_buy_volume": float(item[9]),
            "taker_buy_quote_volume": float(item[10]),
        }

        candles.append(candle)

    return candles


async def get_all_timeframes() -> dict[str, Any]:
    tasks = [
        get_klines(
            interval=timeframe,
            limit=250,
        )
        for timeframe in TIMEFRAMES
    ]

    responses = await asyncio.gather(
        *tasks,
        return_exceptions=True,
    )

    results: dict[str, Any] = {}

    for timeframe, response in zip(
        TIMEFRAMES.keys(),
        responses,
    ):
        results[timeframe] = response

        if isinstance(response, Exception):
            logger.error(
                "Timeframe %s failed: %s",
                timeframe,
                response,
            )

    return results


def candle_direction(
    candle: dict[str, float | int],
) -> str:
    open_price = float(candle["open"])
    close_price = float(candle["close"])

    if close_price > open_price:
        return "🟢 Bullish"

    if close_price < open_price:
        return "🔴 Bearish"

    return "🟡 Neutral"


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    user_id = update.effective_user.id

    await update.message.reply_text(
        "✅ Jan SOL Signal Bot is online!\n\n"
        f"Your Telegram ID: {user_id}\n\n"
        "Available commands:\n"
        "/price - Current SOL market data\n"
        "/timeframes - Check all six timeframes\n"
        "/status - Bot status\n\n"
        "Indicator analysis will be added next."
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
        "📊 Data source: Binance public API\n"
        "🕯 Timeframes: 5m, 15m, 1h, 4h, 8h, 1D\n"
        "🤖 Mode: Alerts only\n"
        "🚫 Automatic trading: Disabled"
    )


async def price(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    message = await update.message.reply_text(
        "⏳ Retrieving SOL market data from Binance..."
    )

    try:
        ticker = await get_ticker_data()

        current_price = float(ticker["lastPrice"])
        price_change_percent = float(
            ticker["priceChangePercent"]
        )
        high_price = float(ticker["highPrice"])
        low_price = float(ticker["lowPrice"])
        quote_volume = float(ticker["quoteVolume"])

        change_icon = (
            "🟢"
            if price_change_percent >= 0
            else "🔴"
        )

        await message.edit_text(
            "💰 SOL/USDT LIVE PRICE\n\n"
            f"Price: ${current_price:,.2f}\n"
            f"{change_icon} 24h change: "
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
            "⚠️ I could not retrieve the SOL price right now.\n"
            "Please try again shortly."
        )


async def timeframes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    message = await update.message.reply_text(
        "⏳ Loading SOL/USDT timeframes..."
    )

    try:
        timeframe_data = await get_all_timeframes()

        lines = [
            "📊 SOL/USDT MULTI-TIMEFRAME CHECK",
            "",
        ]

        successful_count = 0
        failed_count = 0

        for timeframe, label in TIMEFRAMES.items():
            result = timeframe_data.get(timeframe)

            if isinstance(result, Exception):
                failed_count += 1

                lines.append(
                    f"⚠️ {label}: Data request failed"
                )

                continue

            if not result:
                failed_count += 1

                lines.append(
                    f"⚠️ {label}: No candle data received"
                )

                continue

            successful_count += 1

            latest_candle = result[-1]

            close_price = float(
                latest_candle["close"]
            )

            change_percent = calculate_candle_change(
                latest_candle
            )

            direction = candle_direction(
                latest_candle
            )

            lines.append(
                f"{direction} | {label}\n"
                f"Close: ${close_price:,.2f} | "
                f"Candle: {change_percent:+.2f}%"
            )

        lines.extend(
            [
                "",
                f"✅ Loaded: {successful_count}",
                f"⚠️ Failed: {failed_count}",
                "Source: Binance",
            ]
        )

        await message.edit_text(
            "\n".join(lines)
        )

    except Exception as error:
        logger.exception(
            "Timeframe command failed: %s",
            error,
        )

        await message.edit_text(
            "⚠️ The timeframe command encountered an error.\n"
            "Please check the newest Railway deploy logs."
        )
async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.error(
        "Telegram update caused an error",
        exc_info=context.error,
    )


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN is missing from Railway Variables."
        )

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("status", status)
    )

    application.add_handler(
        CommandHandler("price", price)
    )

    application.add_handler(
        CommandHandler("timeframes", timeframes)
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Jan SOL Signal Bot started"
    )

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
