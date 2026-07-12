import os
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# Railway Variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    await update.message.reply_text(
        "✅ Jan SOL Signal Bot is online!\n\n"
        f"Your Telegram ID: {user_id}\n\n"
        "SOL monitoring will be added next."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🟢 Bot status: ONLINE\n"
        "📈 Market: SOL/USDT\n"
        "🤖 Mode: Alerts only\n"
        "🚫 Automatic trading: Disabled"
    )


def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is missing in Railway Variables")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))

    print("Jan SOL Signal Bot started")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
