"""
Polymarket BTC Up or Down 5-Minutes 量化交易 Bot — 啟動入口
版本：v3.0 — 模組化重構
"""

import time
import signal
import threading

import schedule

from btc5m.config import BTC5M_SERIES_SLUG, BTC5M_SERIES_ID, SIGNATURE_TYPE
from btc5m.utils import send
from btc5m.trading import analyze_and_trade, manage_positions
from btc5m.telegram_cmds import start_polling


# ======================================================
# ⏰  排程器
# ======================================================

def _run_in_thread(fn):
    def wrapper():
        threading.Thread(target=fn, daemon=True).start()
    return wrapper

schedule.every(20).seconds.do(_run_in_thread(analyze_and_trade))
schedule.every(10).seconds.do(_run_in_thread(manage_positions))


# ======================================================
# 🚀  主程式入口
# ======================================================

if __name__ == "__main__":
    _shutdown = threading.Event()
    signal.signal(signal.SIGTERM, lambda s, f: _shutdown.set())

    # 啟動 Telegram Bot 監聽
    start_polling()

    send(
        "🚀 Polymarket BTC 5M 量化 Bot 啟動 v3.0\n"
        f"Series: {BTC5M_SERIES_SLUG} (ID: {BTC5M_SERIES_ID})\n"
        f"Signature Type: {SIGNATURE_TYPE}\n"
        "指標: 4H EMA(50) + 5m RSI(14) + MACD 二次確認 + Vol 放量\n"
        "風控: ATR/ADX 自適應 TP/SL | 熔斷機制 | 每日止損/止盈"
    )

    while not _shutdown.is_set():
        try:
            schedule.run_pending()
            time.sleep(2)
        except KeyboardInterrupt:
            break

    send("👋 Bot 已安全關閉")
