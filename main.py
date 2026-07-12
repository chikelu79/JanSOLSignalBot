import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")


def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        },
        timeout=15
    )
    response.raise_for_status()


@app.get("/")
def home():
    return "JanSOLSignalBot is running ✅"


@app.get("/health")
def health():
    return jsonify({"status": "online"})


@app.post("/webhook")
def webhook():
    data = request.get_json(silent=True) or {}

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    symbol = data.get("symbol", "SOLUSDT")
    signal = data.get("signal", "ALERT")
    price = data.get("price", "Unknown")
    timeframe = data.get("timeframe", "Unknown")
    entry = data.get("entry", price)
    stop = data.get("stop", "Not provided")
    target1 = data.get("target1", "Not provided")
    target2 = data.get("target2", "Not provided")
    reason = data.get("reason", "TradingView signal")

    message = (
        f"🚨 <b>{signal} SIGNAL</b>\n\n"
        f"<b>Pair:</b> {symbol}\n"
        f"<b>Timeframe:</b> {timeframe}\n"
        f"<b>Entry:</b> {entry}\n"
        f"<b>Stop:</b> {stop}\n"
        f"<b>Target 1:</b> {target1}\n"
        f"<b>Target 2:</b> {target2}\n\n"
        f"<b>Reason:</b> {reason}\n\n"
        f"⚠️ Signal only. Review before trading."
    )

    send_telegram(message)
    return jsonify({"status": "sent"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
