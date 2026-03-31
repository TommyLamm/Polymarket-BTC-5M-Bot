"""
btc5m.position_manager — 持倉監控（止盈 / 止損 / 超時）
"""

import time
import datetime

from btc5m.config import (
    client,
    POS_MAX_HOLD_SEC,
    open_positions, _recently_closed,
    _positions_lock, _manage_lock,
)
from btc5m.utils import (
    send, _api_call_with_timeout, _parse_orderbook,
    _clean_recently_closed, fetch_live_positions,
    get_usdc_balance, get_conditional_token_balance,
)
from btc5m.trade_exit import _close_position


def _reconcile_settlement_state(token_id: str, pos: dict, hold_seconds: float, source: str) -> bool:
    """
    以 Data API + CLOB token balance 對賬，避免盲目清除本地持倉。
    回傳 True 代表已可安全移除本地追蹤；False 代表仍需繼續監控/重試平倉。
    """
    live_positions = fetch_live_positions()
    if live_positions is None:
        return False

    row = live_positions.get(token_id)
    live_size = float(row.get("size", 0.0) or 0.0) if row else 0.0
    token_bal = get_conditional_token_balance(token_id)
    bal_size = token_bal if token_bal >= 0 else live_size

    if row is not None and bool(row.get("redeemable", False)):
        settle_bal = get_usdc_balance()
        bal_str = f"{settle_bal:.4f} USDC" if settle_bal >= 0 else "查詢失敗"
        mkt_end_dt = pos.get("opened_at") + datetime.timedelta(seconds=pos.get("time_left", 0))
        mkt_label = mkt_end_dt.strftime("%H:%M") + " UTC"
        send(
            f"🏁 市場已結算（可領取）\n"
            f"📋 {pos.get('question', 'N/A')[:40]}\n"
            f"市場結束: {mkt_label}\n"
            f"持倉時間: {int(hold_seconds)}s\n"
            f"來源: {source}\n"
            f"💰 結算後餘額: {bal_str}\n"
            f"請到 Polymarket 進行 Redeem/Claim（本地持倉已清除）"
        )
        with _positions_lock:
            open_positions.pop(token_id, None)
        return True

    if row is None and bal_size <= 0.0001:
        send(
            f"✅ 對賬確認：鏈上已無持倉，清除本地追蹤\n"
            f"📋 {pos.get('question', 'N/A')[:40]}\n"
            f"Token: {token_id[:12]}… | 持倉時間: {int(hold_seconds)}s\n"
            f"來源: {source}"
        )
        with _positions_lock:
            open_positions.pop(token_id, None)
        return True

    if bal_size >= 0:
        with _positions_lock:
            if token_id in open_positions:
                open_positions[token_id]["size"] = max(bal_size, 0.0)

    now_ts = time.time()
    with _positions_lock:
        cur = open_positions.get(token_id, {})
        last_notice = float(cur.get("last_settle_notice_at", 0.0))
        should_notice = (now_ts - last_notice) > 120
        if should_notice and token_id in open_positions:
            open_positions[token_id]["last_settle_notice_at"] = now_ts

    if should_notice:
        send(
            f"⚠️ 結算對賬：仍有未清持倉，繼續監控重試\n"
            f"📋 {pos.get('question', 'N/A')[:40]}\n"
            f"Token: {token_id[:12]}…\n"
            f"來源: {source}\n"
            f"Data API size: {live_size:.4f} | 可用餘額: {bal_size:.4f}"
        )
    return False


def manage_positions():
    """
    檢查所有持倉：達到止盈、止損或超時則觸發平倉。
    使用 _manage_lock 防止並發重入。
    """
    if not _manage_lock.acquire(blocking=False):
        return
    try:
        with _positions_lock:
            tokens = list(open_positions.items())

        if not tokens:
            return

        now_dt = datetime.datetime.now(datetime.timezone.utc)
        for token_id, pos in tokens:
            entry_price = pos["entry_price"]
            hold_seconds = (now_dt - pos["opened_at"]).total_seconds()

            time_since_market_end = hold_seconds - pos["time_left"]
            if time_since_market_end > 180:
                reconciled = _reconcile_settlement_state(
                    token_id, pos, hold_seconds, source="市場結束超時"
                )
                if reconciled:
                    continue

            try:
                book = _api_call_with_timeout(client.get_order_book, token_id)
                best_bid, _ = _parse_orderbook(book)
            except Exception as e:
                print(f"📊 持倉監控 - 訂單簿查詢失敗: {e} | token={token_id[:12]}…")
                if hold_seconds > 330:
                    reconciled = _reconcile_settlement_state(
                        token_id, pos, hold_seconds, source="訂單簿查詢失敗"
                    )
                    if reconciled:
                        continue
                continue

            if best_bid is None:
                if hold_seconds > 330:
                    reconciled = _reconcile_settlement_state(
                        token_id, pos, hold_seconds, source="訂單簿為空"
                    )
                    if reconciled:
                        continue
                else:
                    print(f"⚠️ 訂單簿為空但未超時 ({int(hold_seconds)}s) | token={token_id[:12]}…")
                continue

            tp_pct = float(pos.get("tp_pct", 0.08))
            sl_pct = float(pos.get("sl_pct", 0.10))
            tp_pct = min(max(tp_pct, 0.01), 0.95)
            sl_pct = min(max(sl_pct, 0.01), 0.95)

            tp_target = min(entry_price * (1 + tp_pct), 0.99)
            if tp_target <= entry_price:
                tp_target = entry_price + 0.01

            sl_target = entry_price * (1 - sl_pct)

            unrealized_pct = (best_bid - entry_price) / entry_price
            if unrealized_pct > 0.05:
                max_hold = min(POS_MAX_HOLD_SEC, max(pos["time_left"] - 60, 20))
            elif unrealized_pct < -0.03:
                max_hold = min(POS_MAX_HOLD_SEC, max(pos["time_left"] - 15, 45))
            else:
                max_hold = min(POS_MAX_HOLD_SEC, max(pos["time_left"] - 30, 30))

            reason = None
            if best_bid >= tp_target:
                reason = "🎯 達到動態止盈"
            elif best_bid <= sl_target:
                reason = "🚨 觸發動態止損"
            elif hold_seconds >= max_hold:
                reason = "⏰ 結算規避/超時"

            if reason:
                fee_rate = 0.0156
                est_fee = entry_price * pos["size"] * fee_rate + best_bid * pos["size"] * fee_rate
                net_unrealized_pnl = (best_bid - entry_price) * pos["size"] - est_fee
                upnl_pct = unrealized_pct * 100

                last_reason = pos.get("last_notified_reason")
                last_notify_t = pos.get("last_notified_at", 0)
                reason_changed = (last_reason != reason)
                should_notify = reason_changed or (time.time() - last_notify_t > 120)

                if should_notify:
                    send(f"{reason}\n"
                         f"{'─'*28}\n"
                         f"📋 {pos['question'][:40]}\n"
                         f"進場: {entry_price:.3f} → 現價: {best_bid:.3f}\n"
                         f"淨浮動 PnL (含手續費): {net_unrealized_pnl:+.4f} USDC ({upnl_pct:+.2f}%)\n"
                         f"持倉時間: {int(hold_seconds)}s / {int(max_hold)}s\n"
                         f"TP: {tp_target:.3f} | SL: {sl_target:.3f}\n"
                         f"{'─'*28}")
                    with _positions_lock:
                        if token_id in open_positions:
                            open_positions[token_id]["last_notified_reason"] = reason
                            open_positions[token_id]["last_notified_at"] = time.time()
                else:
                    print(f"🔕 [{reason}] 重試平倉中 ({int(hold_seconds)}s)，抑制重複通知")

                _close_position(token_id, reason, tp_target, sl_target)
                with _positions_lock:
                    if token_id not in open_positions:
                        _recently_closed[token_id] = time.time()
                _clean_recently_closed()
    finally:
        _manage_lock.release()
