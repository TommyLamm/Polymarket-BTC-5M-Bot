"""
btc5m.utils — 通用工具函數（訊息發送、API 包裝、日誌、訂單簿解析）
"""

import os
import csv
import time
import datetime
import random
import threading
import concurrent.futures

import pandas as pd
import requests
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

from btc5m.config import (
    BOT, CHAT_ID, FUNDER_ADDRESS, POSITION_FILE, COOLDOWN_SEC,
    _send_lock, _recently_closed, _API_EXECUTOR, client,
)
from btc5m.observability import log_event, record_api_error, record_rpc_warning


# ======================================================
# 🛠️  通用工具函數
# ======================================================

_RETRYABLE_READ_FUNCS = {
    "get_order_book",
    "get_order",
    "get_trades",
    "get_balance_allowance",
}
_WRITE_FUNCS = {"create_order", "post_order", "cancel"}


def _should_retry_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    terminal_markers = (
        "no orderbook exists for the requested token id",
        "order not found",
        "not found",
    )
    if any(marker in text for marker in terminal_markers):
        return False
    retry_markers = (
        "429",
        "rate limit",
        "too many requests",
        "timeout",
        "timed out",
        "connection",
        "temporarily unavailable",
        "503",
        "502",
        "504",
        "500",
    )
    return any(marker in text for marker in retry_markers)

def send(msg: str):
    """同時輸出至 console 與 Telegram（非阻塞）。"""
    print(msg)
    def _tg():
        with _send_lock:
            try:
                BOT.send_message(CHAT_ID, str(msg), timeout=15)
            except Exception as e:
                print(f"⚠️ Telegram 發送失敗: {e}")
    threading.Thread(target=_tg, daemon=True).start()


def _api_call_with_timeout(fn, *args, timeout=10, retries: int | None = None, **kwargs):
    """在共享執行緒池中以超時方式執行 API 呼叫（含讀取端點重試）。"""
    fn_name = getattr(fn, "__name__", str(fn))
    if retries is None:
        retries = 2 if fn_name in _RETRYABLE_READ_FUNCS else 0
        if fn_name in _WRITE_FUNCS:
            retries = 0
    retries = max(int(retries), 0)

    for attempt in range(retries + 1):
        started_at = time.time()
        future = _API_EXECUTOR.submit(fn, *args, **kwargs)
        try:
            result = future.result(timeout=timeout)
            latency_ms = round((time.time() - started_at) * 1000, 2)
            log_event(
                "api_call",
                api_name=fn_name,
                latency_ms=latency_ms,
                timeout_sec=timeout,
                ok=True,
                attempt=attempt + 1,
            )
            if fn_name == "post_order" and latency_ms >= 2500:
                warning_msg = f"下單廣播延遲：post_order 耗時 {latency_ms} ms"
                print(f"⚠️ {warning_msg}")
                record_rpc_warning(
                    "order_broadcast_delay",
                    warning_msg,
                    source=fn_name,
                    latency_ms=latency_ms,
                )
            return result
        except concurrent.futures.TimeoutError as e:
            latency_ms = round((time.time() - started_at) * 1000, 2)
            error_msg = f"API 呼叫 {fn_name} 逾時 ({timeout}s)"
            record_api_error(
                fn_name,
                error_msg,
                latency_ms=latency_ms,
                timeout_sec=timeout,
                attempt=attempt + 1,
            )
            if fn_name in {"get_balance_allowance", "get_order", "get_order_book"}:
                warning_msg = f"獲取合約數據逾時：{fn_name}（{timeout}s）"
                print(f"⚠️ {warning_msg}")
                record_rpc_warning(
                    "contract_data_timeout",
                    warning_msg,
                    source=fn_name,
                    latency_ms=latency_ms,
                    attempt=attempt + 1,
                )
            if attempt < retries:
                wait_sec = min(2.0, 0.35 * (2 ** attempt)) + random.uniform(0, 0.12)
                record_rpc_warning(
                    "api_retry",
                    f"{fn_name} timeout，{wait_sec:.2f}s 後重試",
                    source=fn_name,
                    attempt=attempt + 1,
                    decision_reason="timeout",
                )
                time.sleep(wait_sec)
                continue
            raise TimeoutError(error_msg) from e
        except Exception as e:
            latency_ms = round((time.time() - started_at) * 1000, 2)
            record_api_error(
                fn_name,
                e,
                latency_ms=latency_ms,
                timeout_sec=timeout,
                attempt=attempt + 1,
            )
            if attempt < retries and _should_retry_error(e):
                wait_sec = min(2.0, 0.35 * (2 ** attempt)) + random.uniform(0, 0.12)
                record_rpc_warning(
                    "api_retry",
                    f"{fn_name} 發生可重試錯誤，{wait_sec:.2f}s 後重試",
                    source=fn_name,
                    attempt=attempt + 1,
                    error_text=str(e),
                    decision_reason="retryable_exception",
                )
                time.sleep(wait_sec)
                continue
            raise


def extract_list_payload(payload, keys: tuple[str, ...] = ("data", "results", "items")) -> list:
    """統一提取列表型 payload，兼容 list 與 dict 包裝格式。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            val = payload.get(key)
            if isinstance(val, list):
                return val
    return []


def extract_object_payload(payload, keys: tuple[str, ...] = ("data", "result")) -> dict:
    """統一提取物件型 payload，兼容 dict 直出與 dict 包裝格式。"""
    if isinstance(payload, dict):
        for key in keys:
            val = payload.get(key)
            if isinstance(val, dict):
                return val
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return {}


def http_get_json(url: str, *, params: dict | None = None, timeout: int = 8, retries: int = 2):
    """
    requests GET + JSON 解析，內建讀取端點重試策略（429/5xx/連線/逾時）。
    """
    retries = max(int(retries), 0)
    for attempt in range(retries + 1):
        started_at = time.time()
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            status = int(resp.status_code)
            if status in {429, 500, 502, 503, 504} and attempt < retries:
                wait_sec = min(2.0, 0.35 * (2 ** attempt)) + random.uniform(0, 0.12)
                record_rpc_warning(
                    "http_retry",
                    f"GET {url} status={status}，{wait_sec:.2f}s 後重試",
                    source="http_get_json",
                    attempt=attempt + 1,
                    status_code=status,
                    url=url,
                    decision_reason="retryable_status_code",
                )
                time.sleep(wait_sec)
                continue
            resp.raise_for_status()
            payload = resp.json()
            latency_ms = round((time.time() - started_at) * 1000, 2)
            log_event(
                "api_call",
                api_name="requests.get",
                url=url,
                latency_ms=latency_ms,
                timeout_sec=timeout,
                ok=True,
                attempt=attempt + 1,
                status_code=status,
            )
            return payload
        except (requests.RequestException, ValueError) as e:
            latency_ms = round((time.time() - started_at) * 1000, 2)
            record_api_error(
                "requests.get",
                e,
                url=url,
                timeout_sec=timeout,
                latency_ms=latency_ms,
                attempt=attempt + 1,
            )
            if attempt < retries and _should_retry_error(e):
                wait_sec = min(2.0, 0.35 * (2 ** attempt)) + random.uniform(0, 0.12)
                record_rpc_warning(
                    "http_retry",
                    f"GET {url} 失敗，{wait_sec:.2f}s 後重試",
                    source="http_get_json",
                    attempt=attempt + 1,
                    error_text=str(e),
                    url=url,
                    decision_reason="retryable_exception",
                )
                time.sleep(wait_sec)
                continue
            raise


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
    except Exception as e:
        print(f"⚠️ 讀取交易日誌失敗: {e}")
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
    if hasattr(resp, "orderId") and resp.orderId:
        return resp.orderId
    if isinstance(resp, dict):
        return resp.get("orderID") or resp.get("orderId") or resp.get("id")
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
        balance_raw = resp.get("balance", 0) if isinstance(resp, dict) else getattr(resp, "balance", 0)
        balance_wei = int(balance_raw or 0)
        return balance_wei / 1_000_000
    except Exception as e:
        print(f"⚠️ 查詢 USDC 餘額失敗: {e}")
        return -1.0


def get_conditional_token_balance(token_id: str) -> float:
    """
    查詢指定 outcome token 的可用餘額（份數）。
    失敗時回傳 -1.0。
    """
    try:
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=str(token_id),
        )
        resp = _api_call_with_timeout(client.get_balance_allowance, params)
        raw = resp.get("balance", 0) if isinstance(resp, dict) else getattr(resp, "balance", 0)
        balance_raw = int(raw or 0)
        # CLOB 返回 1e6 精度
        return balance_raw / 1_000_000
    except Exception as e:
        print(f"⚠️ 查詢條件代幣餘額失敗 ({str(token_id)[:12]}…): {e}")
        return -1.0


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_live_positions(timeout: int = 8) -> dict[str, dict] | None:
    """
    從 Data API 取得目前持倉（未平倉）快照，key 為 token_id(asset)。
    失敗時回傳 None，呼叫端可決定是否降級處理。
    """
    try:
        payload = http_get_json(
            "https://data-api.polymarket.com/positions",
            params={"user": FUNDER_ADDRESS},
            timeout=timeout,
        )
        if isinstance(payload, list):
            rows = payload
        else:
            rows = extract_list_payload(payload, keys=("data", "results", "items", "positions"))

        if not isinstance(rows, list):
            print(f"⚠️ Data API positions 回應格式異常: {type(payload)}")
            return {}
        if not rows:
            return {}

        out: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            token_id = str(row.get("asset") or "").strip()
            if not token_id:
                continue
            normalized = dict(row)
            normalized["size"] = _safe_float(row.get("size"), 0.0)
            normalized["redeemable"] = bool(row.get("redeemable", False))
            out[token_id] = normalized
        return out
    except Exception as e:
        print(f"⚠️ 查詢 Data API 持倉失敗: {e}")
        return None
