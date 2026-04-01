"""
btc5m.order_execution_utils — 交易流程共用工具（狀態正規化、量化、取消驗證、成交輪詢）
"""

import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP

from btc5m.config import client, ORDER_TIMEOUT
from btc5m.utils import _api_call_with_timeout


def _normalize_order_status(raw_status: str) -> str:
    status = str(raw_status or "").strip().upper()
    if status.startswith("ORDER_STATUS_"):
        status = status[len("ORDER_STATUS_"):]
    return status


def _to_decimal(value, default: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _quantize_down(value: float, step: float, minimum: float = 0.0) -> float:
    value_dec = _to_decimal(value, "0")
    step_dec = _to_decimal(step, "0.01")
    min_dec = _to_decimal(minimum, "0")
    if step_dec <= 0:
        return float(max(value_dec, min_dec))
    units = (value_dec / step_dec).to_integral_value(rounding=ROUND_DOWN)
    quantized = units * step_dec
    if quantized < min_dec:
        quantized = min_dec
    return float(quantized)


def _quantize_up(value: float, step: float, minimum: float = 0.0) -> float:
    value_dec = _to_decimal(value, "0")
    step_dec = _to_decimal(step, "0.01")
    min_dec = _to_decimal(minimum, "0")
    if step_dec <= 0:
        return float(max(value_dec, min_dec))
    units = (value_dec / step_dec).to_integral_value(rounding=ROUND_UP)
    quantized = units * step_dec
    if quantized < min_dec:
        quantized = min_dec
    return float(quantized)


def _extract_orderbook_constraints(book) -> tuple[float, float]:
    tick_size = getattr(book, "tick_size", None)
    min_order_size = getattr(book, "min_order_size", None)
    if isinstance(book, dict):
        tick_size = tick_size or book.get("tick_size")
        min_order_size = min_order_size or book.get("min_order_size")

    tick = _to_decimal(tick_size, "0.01")
    if tick <= 0:
        tick = Decimal("0.01")

    min_size = _to_decimal(min_order_size, "0.01")
    if min_size <= 0:
        min_size = Decimal("0.01")

    return float(tick), float(min_size)


def _cancel_order_and_validate(order_id: str, reason: str) -> bool:
    def _is_effectively_not_live() -> bool:
        try:
            st = _api_call_with_timeout(client.get_order, order_id)
            status_raw = (getattr(st, "status", "") if hasattr(st, "status")
                          else st.get("status", ""))
            status = _normalize_order_status(status_raw)
            # LIVE / DELAYED 視為仍在簿上；其餘視為可繼續流程（已完成/已終止/不可再成交）
            return status not in {"LIVE", "DELAYED"}
        except Exception as e:
            err = str(e).lower()
            if "404" in err or "not found" in err:
                return True
            print(f"⚠️ {reason}：取消後狀態核驗失敗 ({order_id[:16]}…): {e}")
            return False

    try:
        resp = _api_call_with_timeout(client.cancel, order_id)
    except Exception as e:
        print(f"⚠️ {reason}：取消訂單失敗 ({order_id[:16]}…): {e}")
        return _is_effectively_not_live()

    if isinstance(resp, dict):
        canceled = resp.get("canceled") or []
        not_canceled = resp.get("not_canceled") or {}
        if isinstance(canceled, list) and order_id in canceled:
            print(f"🗑️ {reason}：已取消訂單 {order_id[:16]}…")
            return True
        if isinstance(not_canceled, dict) and not_canceled:
            msg = not_canceled.get(order_id) or str(not_canceled)
            msg_l = str(msg).lower()
            if "already canceled" in msg_l or "order not found" in msg_l:
                if _is_effectively_not_live():
                    print(f"ℹ️ {reason}：訂單已非 LIVE 狀態 ({order_id[:16]}…): {msg}")
                    return True
            print(f"⚠️ {reason}：取消被拒絕 ({order_id[:16]}…): {msg}")
            return False

    if _is_effectively_not_live():
        print(f"ℹ️ {reason}：取消回應未明確，但訂單已非 LIVE ({order_id[:16]}…)")
        return True

    print(f"⚠️ {reason}：取消回應無法確認 ({order_id[:16]}…): {resp}")
    return False


def _poll_order_matched(oid: str, fallback_price: float, fail_on_unmatched: bool = False) -> tuple:
    """
    輪詢訂單狀態直到成交或超時。
    回傳 (成交成功: bool, 成交估算價: float, 實際成交量: float)
    """
    t0 = time.time()
    matched_status = {"MATCHED", "FILLED"}
    partial_status = {"PARTIALLY_MATCHED", "PARTIALLY_FILLED"}
    failed_status = {"CANCELED", "REJECTED", "EXPIRED", "FAILED"}
    while time.time() - t0 < ORDER_TIMEOUT:
        try:
            st = _api_call_with_timeout(client.get_order, oid)
            if st is None:
                print("🔍 輪詢結果為空，等待下次查詢")
                time.sleep(1)
                continue
            status_raw = (getattr(st, "status", "") if hasattr(st, "status")
                          else st.get("status", ""))
            status = _normalize_order_status(status_raw)

            size_matched = 0.0
            if hasattr(st, "size_matched"):
                size_matched = float(st.size_matched or 0)
            elif isinstance(st, dict):
                size_matched = float(st.get("size_matched") or 0)

            avg_price = fallback_price
            if hasattr(st, "price") and st.price:
                try:
                    avg_price = float(st.price)
                except (ValueError, TypeError):
                    pass
            elif isinstance(st, dict) and st.get("price"):
                try:
                    avg_price = float(st["price"])
                except (ValueError, TypeError):
                    pass

            print(f"🔍 訂單狀態: {status} | size_matched: {size_matched}")

            if status in failed_status:
                return False, fallback_price, 0.0

            if status in matched_status:
                return True, avg_price, size_matched

            if status in partial_status and size_matched > 0:
                return True, avg_price, size_matched

            if fail_on_unmatched and status == "UNMATCHED" and size_matched <= 0:
                return False, fallback_price, 0.0
        except Exception as e:
            print(f"🔍 輪詢異常: {e}")
        time.sleep(2)
    return False, fallback_price, 0.0
