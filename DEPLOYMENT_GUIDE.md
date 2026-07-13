# JanSOLSignalBot deployment guide

## What this build includes

- Binance symbol-aware price and candle data
- Six timeframes: 5m, 15m, 1h, 4h, 8h and 1d
- BTC and ETH context
- BTC dominance and total crypto market change
- VIX context
- Live Crypto Fear & Greed with a safe fallback
- Level-first alerts: WATCH, PREPARE, ENTRY and DO NOT CHASE
- Position alerts: breakeven, TP1, TP2, TP3, invalidation and exit
- US session awareness in alert messages
- Multi-pair watchlist monitoring

The bot provides analysis and alerts only. It does not place orders.

## Railway variables

Required:

- `TELEGRAM_BOT_TOKEN`

Recommended:

- `TELEGRAM_CHAT_ID`
- `COINGECKO_API_KEY`
- `MONITOR_INTERVAL_SECONDS=30`
- `INITIAL_MONITOR_DELAY_SECONDS=15`
- `MAX_MONITORED_PAIRS=8`
- `SCAN_CONCURRENCY=2`

Railway start command:

```text
python main.py
```

## Uploading to GitHub

### Safest method: GitHub Desktop on Mac

1. Download and unzip this package.
2. Open GitHub Desktop and clone/open `JanSOLSignalBot`.
3. In Finder, open both the downloaded package and the local repository folder.
4. Copy every file from this package into the local repository folder.
5. Choose **Replace** when macOS asks about files with matching names.
6. In GitHub Desktop, review the changed files.
7. Commit with `Install validated level-first alert build`.
8. Click **Push origin**.
9. Railway should deploy the new commit automatically.

Do not delete the repository first. Overwrite matching files instead.

### Browser upload

1. Open the GitHub repository.
2. Select **Add file > Upload files**.
3. Drag all files from inside the extracted folder into the upload area.
4. Commit the upload.

GitHub's browser upload may not reliably replace an existing file in every mobile workflow. GitHub Desktop is preferred.

## First Telegram tests

Run one command at a time:

```text
/start
/status
/price
/market
/scan
/watchlist
/monitor on
```

## Duplicate bot warning

Only one Python bot process may use the Telegram token at once. Telegram can remain open on several devices, but do not run `python main.py` on a laptop while Railway is also running it.
