"""
btc5m.observability — 交易觀測與統計（事件記錄、異常分類、Missed Trades 分析）
"""

from __future__ import annotations

import json
import os
import time
import uuid
import threading
import datetime
from collections import Counter, deque
from typing import Any

_LOCK = threading.Lock()
_EVENTS = deque(maxlen=8000)
_ENABLED = True
_EVENTS_PATH: str | None = None
_SESSION_LABEL = "default"

MISSED_REASON_LABELS: dict[str, str] = {
    "market_fetch_empty": "找不到活躍市場",
    "market_not_accepting_orders": "市場未開放下單",
    "market_neg_risk": "市場為 negRisk",
    "time_left_lt_60": "剩餘時間少於 60 秒",
    "token_unresolved": "無法解析目標代幣",
    "already_holding_token": "該代幣已持倉",
    "token_cooldown": "代幣冷卻中",
    "orderbook_fetch_error": "訂單簿查詢失敗",
    "orderbook_no_liquidity": "訂單簿無流動性",
    "ask_not_atm": "價格不在 ATM 範圍",
    "spread_out_of_range": "價差不符條件",
    "unit_cost_invalid": "單位成本異常",
    "max_affordable_size_zero": "資金不足以達最小可下單量",
    "min_size_over_cap": "最小下單量超過風險上限",
    "quantized_size_below_min": "量化後下單量低於最小值",
    "cost_over_cap": "預估成本超過資金上限",
    "entry_actual_cost_exceeds_cap": "成交後實際成本超過上限",
    "order_timeout_unfilled": "下單後超時未成交",
    "post_order_rejected": "送單被拒絕",
    "missing_order_id": "送單回應缺少訂單 ID",
}


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def configure_observability(
    enabled: bool = True,
    events_path: str | None = None,
    session_label: str | None = None,
):
    global _ENABLED, _EVENTS_PATH, _SESSION_LABEL
    with _LOCK:
        _ENABLED = bool(enabled)
        _EVENTS_PATH = events_path
        if session_label:
            _SESSION_LABEL = str(session_label)


def reset_events():
    with _LOCK:
        _EVENTS.clear()


def classify_error_text(error_text: str) -> str:
    text = str(error_text or "").lower()
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return "rate_limit"
    if "invalid signature" in text or ("signature" in text and "invalid" in text):
        return "invalid_signature"
    if "timeout" in text or "timed out" in text or "逾時" in text:
        return "timeout"
    if "not enough balance" in text or "allowance" in text:
        return "insufficient_balance_or_allowance"
    if "fak orders are partially filled or killed" in text or "no orders found to match" in text:
        return "fak_no_match"
    return "unknown"


def _write_event_file(event: dict[str, Any]):
    if not _EVENTS_PATH:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(_EVENTS_PATH)), exist_ok=True)
        with open(_EVENTS_PATH, mode="a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"⚠️ 觀測事件寫檔失敗: {e}")


def log_event(stage: str, **fields) -> dict[str, Any]:
    if not _ENABLED:
        return {}
    event = {
        "ts": time.time(),
        "iso_ts": _iso_now(),
        "session": _SESSION_LABEL,
        "stage": stage,
        **fields,
    }
    with _LOCK:
        _EVENTS.append(event)
    _write_event_file(event)
    return event


def record_api_error(source: str, error: Any, **fields) -> dict[str, Any]:
    error_text = str(error)
    error_kind = classify_error_text(error_text)
    return log_event(
        "api_error",
        source=source,
        error_kind=error_kind,
        error_text=error_text,
        **fields,
    )


def record_rpc_warning(warning_type: str, message: str, **fields) -> dict[str, Any]:
    return log_event(
        "rpc_warning",
        warning_type=warning_type,
        message=message,
        **fields,
    )


def record_missed_trade(reason_code: str, reason_text: str = "", **fields) -> dict[str, Any]:
    return log_event(
        "missed_trade",
        reason_code=reason_code,
        reason_label=MISSED_REASON_LABELS.get(reason_code, reason_code),
        reason_text=reason_text,
        **fields,
    )


def record_order_attempt(scope: str, side: str, **fields) -> str:
    attempt_id = uuid.uuid4().hex[:12]
    log_event(
        "order_attempt",
        attempt_id=attempt_id,
        scope=scope,
        side=side,
        **fields,
    )
    return attempt_id


def record_order_result(
    attempt_id: str,
    scope: str,
    success: bool,
    order_id: str | None = None,
    error_text: str = "",
    **fields,
):
    error_kind = classify_error_text(error_text) if error_text else ""
    return log_event(
        "order_result",
        attempt_id=attempt_id,
        scope=scope,
        success=bool(success),
        order_id=order_id or "",
        error_text=error_text,
        error_kind=error_kind,
        **fields,
    )


def get_events(
    since_ts: float | None = None,
    stage: str | None = None,
) -> list[dict[str, Any]]:
    with _LOCK:
        snapshot = list(_EVENTS)
    out = []
    for row in snapshot:
        if since_ts is not None and float(row.get("ts", 0.0)) < since_ts:
            continue
        if stage and row.get("stage") != stage:
            continue
        out.append(row)
    return out


def count_order_attempts(since_ts: float | None = None, scope: str | None = None) -> int:
    events = get_events(since_ts=since_ts, stage="order_attempt")
    if scope is None:
        return len(events)
    return sum(1 for e in events if e.get("scope") == scope)


def summarize_missed_trades(window_sec: int = 3600) -> dict[str, Any]:
    now = time.time()
    rows = get_events(since_ts=now - max(window_sec, 1), stage="missed_trade")
    total = len(rows)
    if total <= 0:
        return {
            "window_sec": window_sec,
            "total": 0,
            "top_reason_code": "",
            "top_reason_label": "",
            "top_ratio_pct": 0.0,
            "by_reason": [],
            "headline": "過去一小時無 Missed Trades 記錄",
            "recommendation": "目前無需調整。",
        }

    counter = Counter(str(r.get("reason_code") or "unknown") for r in rows)
    by_reason = []
    for code, count in counter.most_common():
        ratio = count / total * 100
        by_reason.append(
            {
                "reason_code": code,
                "reason_label": MISSED_REASON_LABELS.get(code, code),
                "count": count,
                "ratio_pct": round(ratio, 2),
            }
        )

    top = by_reason[0]
    top_code = top["reason_code"]
    top_ratio = top["ratio_pct"]
    recommendation = _recommend_for_reason(top_code, top_ratio)
    headline = (
        f"過去一小時內有 {top_ratio:.1f}% 的信號因「{top['reason_label']}」被過濾，"
        f"共 {top['count']}/{total} 次。"
    )
    return {
        "window_sec": window_sec,
        "total": total,
        "top_reason_code": top_code,
        "top_reason_label": top["reason_label"],
        "top_ratio_pct": top_ratio,
        "by_reason": by_reason,
        "headline": headline,
        "recommendation": recommendation,
    }


def _recommend_for_reason(reason_code: str, ratio_pct: float) -> str:
    if reason_code == "ask_not_atm":
        if ratio_pct >= 70:
            return "ATM 篩選過嚴，建議檢視進場價格範圍（如 0.30~0.70）與市場窗口選擇。"
        return "可觀察是否需微調 ATM 進場範圍。"
    if reason_code == "spread_out_of_range":
        return "價差條件命中率偏低，建議依近期流動性微調 MIN/MAX_SPREAD。"
    if reason_code in {"time_left_lt_60", "market_not_accepting_orders"}:
        return "執行時點偏晚，建議提前掃描或提高排程頻率。"
    if reason_code in {"max_affordable_size_zero", "min_size_over_cap", "cost_over_cap"}:
        return "下單資金上限與市場最小量不匹配，建議調整 MAX_USD 或最小單位策略。"
    return "建議持續追蹤該原因與市場條件，必要時調整進場規則。"


def summarize_api_health(window_sec: int = 3600) -> dict[str, Any]:
    now = time.time()
    api_errors = get_events(since_ts=now - max(window_sec, 1), stage="api_error")
    rpc_warnings = get_events(since_ts=now - max(window_sec, 1), stage="rpc_warning")
    err_counter = Counter(str(e.get("error_kind") or "unknown") for e in api_errors)
    return {
        "window_sec": window_sec,
        "api_error_total": len(api_errors),
        "rpc_warning_total": len(rpc_warnings),
        "error_kinds": dict(err_counter),
        "recent_api_errors": api_errors[-10:],
        "recent_rpc_warnings": rpc_warnings[-10:],
    }
