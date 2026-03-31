"""
btc5m.utils — 通用工具函數（訊息發送、API 包裝、日誌、訂單簿解析）
"""

import os
import csv
import time
import datetime
import threading
import concurrent.futures

import pandas as pd
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

from btc5m.config import (
    BOT, CHAT_ID, POSITION_FILE, COOLDOWN_SEC,
    _send_lock, _recently_closed, _API_EXECUTOR, client,
)


# ======================================================
# 🛠️  通用工具函數
# ======================================================

def send(msg: str):
    """同時輸出至 console 與 Telegram（非阻塞）。"""
    print(msg)
    def _tg():
        with _send_lock:
            try:
                BOT.send_message(CHAT_ID, str(msg), timeout=15)
            except Exception:
                pass
    threading.Thread(target=_tg, daemon=True).start()


def _api_call_with_timeout(fn, *args, timeout=10, **kwargs):
    """在共享執行緒池中以超時方式執行 API 呼叫。"""
    future = _API_EXECUTOR.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise TimeoutError(f"API 呼叫 {fn.__name__} 逾時 ({timeout}s)")


def log_trade(data: dict):
    """將交易紀錄追加寫入 CSV 日誌。"""
    FIELDS = ["date", "timestamp", "token_id", "side", "entry_price",
              "exit_price", "size", "slippage_pct", "realized_pnl", "fees", "hold_time", "status"]
    file_exists = os.path.isfile(POSITION_FILE)
    with open(POSITION_FILE, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow({k: data.get(k) for k in FIELDS})


def get_daily_realized_pnl() -> float:
    """讀取今日已實現損益合計。"""
    if not os.path.exists(POSITION_FILE):
        return 0.0
    try:
        df = pd.read_csv(POSITION_FILE)
        today = datetime.date.today().isoformat()
        return float(df[df["date"] == today]["realized_pnl"].sum())
    except Exception:
        return 0.0


def _parse_orderbook(book) -> tuple[float | None, float | None]:
    """從 ClobClient 訂單簿物件解析最佳買價（bid）與最佳賣價（ask）。"""
    bids = getattr(book, "bids", []) if hasattr(book, "bids") else book.get("bids", [])
    asks = getattr(book, "asks", []) if hasattr(book, "asks") else book.get("asks", [])
    if not bids or not asks:
        return None, None
    def _p(item):
        return float(item.price) if hasattr(item, "price") else float(item["price"])
    return max(_p(b) for b in bids), min(_p(a) for a in asks)


def _clean_recently_closed():
    """清理 _recently_closed 中已超過冷卻期的條目，防止記憶體持續增長。"""
    now = time.time()
    expired = [k for k, v in _recently_closed.items() if now - v >= COOLDOWN_SEC]
    for k in expired:
        _recently_closed.pop(k, None)


def _get_order_id(resp) -> str | None:
    """
    統一提取下單回應中的訂單 ID。
    相容物件（.orderID）與 dict（"orderID" / "id"）兩種格式。
    """
    if hasattr(resp, "orderID") and resp.orderID:
        return resp.orderID
    if isinstance(resp, dict):
        return resp.get("orderID") or resp.get("id")
    return None


def get_usdc_balance() -> float:
    """
    通過 Polymarket CLOB API 查詢 USDC 餘額（使用 API 憑證，無需 RPC）。
    返回 USDC 金額（浮點數），失敗時返回 -1.0。

    速率限制：200次/10秒 → 每10秒查一次絕對安全。
    """
    try:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        resp = _api_call_with_timeout(client.get_balance_allowance, params)
        # balance 單位是 wei（USDC 6位小數），轉換為 USDC
        balance_wei = int(resp.get("balance", 0))
        return balance_wei / 1_000_000
    except Exception as e:
        print(f"⚠️ 查詢 USDC 餘額失敗: {e}")
        return -1.0
