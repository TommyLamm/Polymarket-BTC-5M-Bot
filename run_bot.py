"""
Polymarket BTC Up or Down 5-Minutes 量化交易 Bot — 啟動入口
版本：v3.0 — 模組化重構
"""

import time
import signal
import threading

import schedule

import btc5m.config as cfg
from btc5m.config import BTC5M_SERIES_SLUG, BTC5M_SERIES_ID, SIGNATURE_TYPE
from btc5m.utils import send, get_usdc_balance
from btc5m.trading import analyze_and_trade, manage_positions
from btc5m.telegram_cmds import start_polling


# ======================================================
# ⏰  排程器
# ======================================================

def _run_in_thread(fn):
    def wrapper():
        threading.Thread(target=fn, daemon=True).start()
    return wrapper


def _configure_schedule():
    schedule.clear()
    schedule.every(float(cfg.ANALYZE_INTERVAL_SEC)).seconds.do(_run_in_thread(analyze_and_trade))
    schedule.every(float(cfg.MANAGE_INTERVAL_SEC)).seconds.do(_run_in_thread(manage_positions))


# ======================================================
# 🚀  主程式入口
# ======================================================

if __name__ == "__main__":
    _shutdown = threading.Event()
    signal.signal(signal.SIGTERM, lambda s, f: _shutdown.set())

    _configure_schedule()

    # 啟動 Telegram Bot 監聽
    start_polling()

    # 首次查詢餘額，動態更新 START_CAPITAL
    initial_balance = get_usdc_balance()
    if initial_balance >= 0:
        cfg.START_CAPITAL = initial_balance
        print(f"💰 初始 USDC 餘額: {initial_balance:.4f} USDC → 已更新至 START_CAPITAL")
        bal_line = f"💰 開始餘額: {initial_balance:.4f} USDC\n"
    else:
        print("⚠️ 無法查詢初始餘額，使用預設 START_CAPITAL")
        bal_line = ""

    send(
        "🚀 Polymarket BTC 5M 量化 Bot 啟動 v3.0\n"
        f"Series: {BTC5M_SERIES_SLUG} (ID: {BTC5M_SERIES_ID})\n"
        f"Signature Type: {SIGNATURE_TYPE}\n"
        f"{bal_line}"
        "指標: 4H EMA(50) + 5m RSI(14) + MACD 二次確認 + Vol 放量\n"
        "風控: ATR/ADX 自適應 TP/SL | 熔斷機制 | 每日止損/止盈\n"
        f"排程: analyze={cfg.ANALYZE_INTERVAL_SEC}s | "
        f"manage={cfg.MANAGE_INTERVAL_SEC}s | tick={cfg.SCHEDULER_TICK_SEC}s"
    )

    while not _shutdown.is_set():
        try:
            schedule.run_pending()
            time.sleep(max(0.02, float(cfg.SCHEDULER_TICK_SEC)))
        except KeyboardInterrupt:
            break

    send("👋 Bot 已安全關閉")
