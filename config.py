import os


# =========================================================
# TELEGRAM
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
# BINANCE MARKET DATA
# =========================================================

BINANCE_BASE_URL = (
    "https://data-api.binance.vision"
)

DEFAULT_SYMBOL = "SOLUSDT"

CANDLE_LIMIT = 300


# =========================================================
# TIMEFRAMES
# =========================================================

TIMEFRAMES = {
    "5m": {
        "label": "5 minutes",
        "weight": 0.08,
    },
    "15m": {
        "label": "15 minutes",
        "weight": 0.14,
    },
    "1h": {
        "label": "1 hour",
        "weight": 0.20,
    },
    "4h": {
        "label": "4 hours",
        "weight": 0.23,
    },
    "8h": {
        "label": "8 hours",
        "weight": 0.17,
    },
    "1d": {
        "label": "1 day",
        "weight": 0.18,
    },
}


# =========================================================
# AUTOMATIC MONITORING
# =========================================================

MONITOR_INTERVAL_SECONDS = 30

INITIAL_MONITOR_DELAY_SECONDS = 15

ALERT_COOLDOWN_SECONDS = 30 * 60

MINIMUM_AVAILABLE_TIMEFRAMES = 4


# =========================================================
# SIGNAL THRESHOLDS
# =========================================================

WATCH_THRESHOLD = 62

CONFIRMED_THRESHOLD = 74

STRONG_THRESHOLD = 84


# =========================================================
# TRADE PLAN SETTINGS
# =========================================================

MINIMUM_REWARD_RISK = 1.5

ATR_STOP_MULTIPLIER = 1.5

TP1_REWARD_MULTIPLIER = 1.25

TP2_REWARD_MULTIPLIER = 2.0

TP3_REWARD_MULTIPLIER = 3.0
