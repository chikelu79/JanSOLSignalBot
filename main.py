import asyncio
import logging
import os

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)
from tradingview_ta import Exchange, Interval, TA_Handler


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def get_sol_analysis():
    handler = TA_Handler(
        symbol="SOLUSDT",
        screener="crypto",
        exchange=Exchange.BINANCE,
        interval=Interval.INTERVAL_15_MINUTES,
        timeout=15,
    )

    return handler.get_analysis()


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user_id = update.effective_user.id

    await update.message.reply_text(
        "✅ Jan SOL Signal Bot is online!\n\n"
        f"Your Telegram ID: {user_id}\n\n"
        "Available commands:\n"
        "/price - Current SOL price\n"
        "/analysis - TradingView analysis\n"
        "/status - Bot status"
    )


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.message.reply_text(
        "🟢 Bot status: ONLINE\n"
        "📈 Market: SOL/USDT\n"
        "🕯 Timeframe: 15 minutes\n"
        "📡 Data: TradingView\n"
        "🤖 Mode: Alerts only\n"
        "🚫 Automatic trading: Disabled"
    )


async def price(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = await update.message.reply_text(
        "⏳ Retrieving SOL price from TradingView..."
    )

    try:
        analysis = await asyncio.to_thread(get_sol_analysis)
        current_price = analysis.indicators.get("close")

        if current_price is None:
            raise ValueError("TradingView returned no closing price.")

        await message.edit_text(
            "💰 SOL LIVE PRICE\n\n"
            f"SOL/USDT: ${current_price:,.2f}\n"
            "Source: TradingView\n"
            "Exchange: Binance\n"
            "Timeframe: 15 minutes"
        )

    except Exception as error:
        logger.exception("Price request failed: %s", error)

        await message.edit_text(
            "⚠️ I could not retrieve the SOL price right now.\n"
            "Please try again shortly."
        )


async def analysis_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = await update.message.reply_text(
        "🔍 Running TradingView analysis..."
    )

    try:
        analysis = await asyncio.to_thread(get_sol_analysis)

        summary = analysis.summary
        indicators = analysis.indicators

        recommendation = summary.get("RECOMMENDATION", "NEUTRAL")
        buy_count = summary.get("BUY", 0)
        neutral_count = summary.get("NEUTRAL", 0)
        sell_count = summary.get("SELL", 0)

        current_price = indicators.get("close")
        rsi = indicators.get("RSI")
        ema20 = indicators.get("EMA20")
        ema50 = indicators.get("EMA50")
        ema200 = indicators.get("EMA200")
        macd = indicators.get("MACD.macd")
        macd_signal = indicators.get("MACD.signal")

        price_text = (
            f"${current_price:,.2f}"
            if isinstance(current_price, (int, float))
            else "Unavailable"
        )

        rsi_text = (
            f"{rsi:.2f}"
            if isinstance(rsi, (int, float))
            else "Unavailable"
        )

        ema20_text = (
            f"${ema20:,.2f}"
            if isinstance(ema20, (int, float))
            else "Unavailable"
        )

        ema50_text = (
            f"${ema50:,.2f}"
            if isinstance(ema50, (int, float))
            else "Unavailable"
        )

        ema200_text = (
            f"${ema200:,.2f}"
            if isinstance(ema200, (int, float))
            else "Unavailable"
        )

        macd_text = (
            f"{macd:.4f}"
            if isinstance(macd, (int, float))
            else "Unavailable"
        )

        macd_signal_text = (
            f"{macd_signal:.4f}"
            if isinstance(macd_signal, (int, float))
            else "Unavailable"
        )

        if "STRONG_BUY" in recommendation:
            signal_emoji = "🚀"
        elif recommendation == "BUY":
            signal_emoji = "🟢"
        elif "STRONG_SELL" in recommendation:
            signal_emoji = "🔻"
        elif recommendation == "SELL":
            signal_emoji = "🔴"
        else:
            signal_emoji = "🟡"

        await message.edit_text(
            "📊 TRADINGVIEW SOL ANALYSIS\n\n"
            f"{signal_emoji} Signal: {recommendation.replace('_', ' ')}\n"
            f"💰 Price: {price_text}\n"
            "🕯 Timeframe: 15 minutes\n\n"
            f"✅ Buy indicators: {buy_count}\n"
            f"➖ Neutral indicators: {neutral_count}\n"
            f"❌ Sell indicators: {sell_count}\n\n"
            f"RSI: {rsi_text}\n"
            f"EMA 20: {ema20_text}\n"
            f"EMA 50: {ema50_text}\n"
            f"EMA 200: {ema200_text}\n"
            f"MACD: {macd_text}\n"
            f"MACD signal: {macd_signal_text}\n\n"
            "Source: TradingView via tradingview-ta\n"
            "⚠️ Analysis only, not financial advice."
        )

    except Exception as error:
        logger.exception("TradingView analysis failed: %s", error)

        await message.edit_text(
            "⚠️ TradingView analysis could not be retrieved.\n"
            "Please try again shortly."
        )


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.exception(
        "Telegram update caused an error:",
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

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("price", price))
    application.add_handler(
        CommandHandler("analysis", analysis_command)
    )

    application.add_error_handler(error_handler)

    logger.info("Jan SOL Signal Bot started")

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
