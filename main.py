import logging
import os
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

app = FastAPI(title="Jan SOL Signal Bot")


async def send_telegram_message(
    chat_id: str,
    text: str,
) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

        response.raise_for_status()


@app.get("/")
async def home() -> dict[str, str]:
    return {
        "status": "online",
        "bot": "Jan SOL Signal Bot",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.post("/telegram")
async def telegram_webhook(request: Request) -> JSONResponse:
    update: dict[str, Any] = await request.json()

    message = update.get("message") or update.get("edited_message")

    if not message:
        return JSONResponse({"ok": True})

    chat = message.get("chat", {})
    user = message.get("from", {})
    chat_id = str(chat.get("id", ""))
    user_id = user.get("id", "Unknown")
    text = str(message.get("text", "")).strip().lower()

    try:
        if text.startswith("/start"):
            response_text = (
                "✅ <b>Jan SOL Signal Bot is online!</b>\n\n"
                f"Your Telegram ID: <code>{user_id}</code>\n\n"
                "<b>Available commands</b>\n"
                "/status - Bot status\n"
                "/chatid - Show this chat ID\n"
                "/help - Show available commands"
            )

        elif text.startswith("/status"):
            response_text = (
                "🟢 <b>Bot status: ONLINE</b>\n"
                "📈 Market: SOL/USDT\n"
                "📡 Alerts: TradingView webhooks\n"
                "🏦 Exchange: Binance\n"
                "🤖 Mode: Alerts only\n"
                "🚫 Automatic trading: Disabled"
            )

        elif text.startswith("/chatid"):
            response_text = (
                "Your Telegram chat ID is:\n"
                f"<code>{chat_id}</code>"
            )

        elif text.startswith("/help"):
            response_text = (
                "<b>Jan SOL Signal Bot commands</b>\n\n"
                "/start - Start the bot\n"
                "/status - Check bot status\n"
                "/chatid - Show chat ID\n"
                "/help - Show this menu"
            )

        else:
            return JSONResponse({"ok": True})

        await send_telegram_message(chat_id, response_text)

    except Exception:
        logger.exception("Telegram command failed")

    return JSONResponse({"ok": True})


@app.post("/tradingview")
async def tradingview_webhook(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
) -> JSONResponse:
    if not WEBHOOK_SECRET:
        raise HTTPException(
            status_code=500,
            detail="WEBHOOK_SECRET is not configured",
        )

    if x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook secret",
        )

    if not TELEGRAM_CHAT_ID:
        raise HTTPException(
            status_code=500,
            detail="TELEGRAM_CHAT_ID is not configured",
        )

    try:
        payload = await request.json()
    except Exception:
        raw_body = (await request.body()).decode(
            "utf-8",
            errors="replace",
        )
        payload = {"message": raw_body}

    if isinstance(payload, dict):
        symbol = payload.get("symbol", "SOLUSDT")
        exchange = payload.get("exchange", "BINANCE")
        timeframe = payload.get("timeframe", "Unknown")
        signal = payload.get("signal", "ALERT")
        price = payload.get("price", "Unknown")
        message_text = payload.get("message", "")

        alert_text = (
            "🚨 <b>TRADINGVIEW ALERT</b>\n\n"
            f"Signal: <b>{signal}</b>\n"
            f"Market: {exchange}:{symbol}\n"
            f"Price: {price}\n"
            f"Timeframe: {timeframe}"
        )

        if message_text:
            alert_text += f"\n\n{message_text}"
    else:
        alert_text = (
            "🚨 <b>TRADINGVIEW ALERT</b>\n\n"
            f"{payload}"
        )

    try:
        await send_telegram_message(
            TELEGRAM_CHAT_ID,
            alert_text,
        )
    except Exception as error:
        logger.exception("TradingView alert failed")
        raise HTTPException(
            status_code=502,
            detail="Telegram delivery failed",
        ) from error

    logger.info("TradingView alert delivered")

    return JSONResponse(
        {
            "ok": True,
            "message": "Alert delivered",
        }
    )


@app.on_event("startup")
async def startup_event() -> None:
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is missing")

    if not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_CHAT_ID is missing")

    if not WEBHOOK_SECRET:
        logger.warning("WEBHOOK_SECRET is missing")

    logger.info("Jan SOL Signal Bot web server started")
