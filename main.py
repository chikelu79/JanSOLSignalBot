import asyncio
import os

import aiohttp
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes


TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
SYMBOL = "SOLUSDT"


async def get_sol_price() -> float:
    params = {"symbol": SYMBOL}

    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(BINANCE_PRICE_URL, params=params) as response:
            response.raise_for_status()
            data = await response.json()

    return float(data["price"])


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user_id = update.effective_user.id

    await update.message.reply_text(
        "✅ Jan SOL Signal Bot is online!\n\n"
        f"Your Telegram ID: {user_id}\n\n"
        "Commands:\n"
        "/price - Current SOL price\n"
        "/status - Bot status"
    )


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.message.reply_text(
        "🟢 Bot status: ONLINE\n"
        "📈 Market: SOL/USDT\n"
        "🤖 Mode: Live monitoring\n"
        "🚫 Automatic trading: Disabled"
    )


async def price(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    try:
        sol_price = await get_sol_price()

        await update.message.reply_text(
            "💰 SOL LIVE PRICE\n\n"
            f"SOL/USDT: ${sol_price:,.2f}"
        )

    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as error:
        print(f"Price request failed: {error}")

        await update.message.reply_text(
            "⚠️ I could not retrieve the SOL price right now. "
            "Please try again shortly."
        )


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN is missing from Railway Variables"
        )

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("price", price))

    print("Jan SOL Signal Bot started")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
