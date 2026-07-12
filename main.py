import os
import asyncio
import logging

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from tradingview_ta import TA_Handler, Interval, Exchange


logging.basicConfig(
    format="
