"""
btc5m.position_manager — 持倉監控（止盈 / 止損 / 超時）
"""

import time
import datetime

from btc5m.config import (
    client,
    POS_MAX_HOLD_SEC,
    TP_BASE_PCT, SL_BASE_PCT,
    STOPLOSS_CONFIRM_COUNT, STOPLOSS_EMERGENCY_EXTRA_PCT,
    ENDGAME_GRACE_SEC, ENDGAME_ONLY_EMERGENCY_SL, ENDGAME_NOTIFY_INTERVAL_SEC,
    open_positions, _recently_closed,
    _positions_lock, _manage_lock,
)
from btc5m.utils import (
    send, _api_call_with_timeout, _parse_orderbook,
    _clean_recently_closed, fetch_live_positions,
    get_usdc_balance, get_conditional_token_balance,
)
from btc5m.trade_exit import _close_position
from btc5m.observability import record_api_error, record_position_event


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
        record_position_event(
            "settlement_redeemable_cleared",
            message="position manager reconcile cleared redeemable position",
            token_id=token_id,
            source=source,
            hold_seconds=hold_seconds,
            live_size=live_size,
            balance_size=bal_size,
            decision_reason="redeemable_true",
        )
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
        record_position_event(
            "settlement_reconciled_closed",
            message="position manager reconcile removed local position",
            token_id=token_id,
            source=source,
            hold_seconds=hold_seconds,
            live_size=live_size,
            balance_size=bal_size,
            decision_reason="row_missing_and_balance_zero",
        )
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
        if source in {"訂單簿查詢失敗", "訂單簿為空", "市場結束超時", "dust 殘量待結算"}:
            record_position_event(
                "settlement_no_orderbook",
                message="settlement reconcile notice",
                token_id=token_id,
                source=source,
                hold_seconds=hold_seconds,
                live_size=live_size,
                balance_size=bal_size,
            )
        record_position_event(
            "settlement_reconcile_pending",
            message="position manager reconcile pending",
            token_id=token_id,
            source=source,
            hold_seconds=hold_seconds,
            live_size=live_size,
            balance_size=bal_size,
            decision_reason="position_still_exists",
        )
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
            hold_seconds = (now_dt - pos["opened_at"]).total_seconds()
            if pos.get("exit_state") == "dust_pending_settlement":
                reconciled = _reconcile_settlement_state(
                    token_id, pos, hold_seconds, source="dust 殘量待結算"
                )
                with _positions_lock:
                    latest = open_positions.get(token_id)
                    dust_min_size = float((latest or {}).get("dust_min_order_size", 0.0) or 0.0)
                    latest_size = float((latest or {}).get("size", 0.0) or 0.0)
                if latest is not None and dust_min_size > 0 and latest_size + 1e-9 >= dust_min_size:
                    with _positions_lock:
                        cur = open_positions.get(token_id)
                        if cur is None:
                            continue
                        cur.pop("exit_state", None)
                        cur.pop("exit_state_reason", None)
                        cur.pop("dust_notified_at", None)
                        cur.pop("dust_poll_logged_at", None)
                        recovered_pos = cur.copy()
                    send(
                        f"✅ dust 持倉恢復可交易，重新啟動平倉邏輯\n"
                        f"Token: {token_id[:12]}… | size: {latest_size:.4f}"
                    )
                    record_position_event(
                        "dust_recovered_tradeable",
                        message="dust position recovered and can trade again",
                        token_id=token_id,
                        hold_seconds=hold_seconds,
                        recovered_size=latest_size,
                        min_order_size=dust_min_size,
                        source="position_manager",
                    )
                    pos = recovered_pos
                if not reconciled:
                    with _positions_lock:
                        cur = open_positions.get(token_id, {})
                        last_log_ts = float(cur.get("dust_poll_logged_at", 0.0) or 0.0)
                        now_ts = time.time()
                        should_log = (now_ts - last_log_ts) >= 120
                        if should_log and token_id in open_positions:
                            open_positions[token_id]["dust_poll_logged_at"] = now_ts
                    if should_log:
                        record_position_event(
                            "exit_dust_below_min_size",
                            message="position manager polling dust settlement",
                            token_id=token_id,
                            hold_seconds=hold_seconds,
                            source="position_manager",
                        )
                if pos.get("exit_state") == "dust_pending_settlement":
                    continue

            entry_price = pos["entry_price"]

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
                record_api_error(
                    "position_manager_get_order_book",
                    e,
                    token_id=token_id,
                    hold_seconds=hold_seconds,
                )
                if "no orderbook exists for the requested token id" in str(e).lower():
                    record_position_event(
                        "settlement_no_orderbook",
                        message="position manager get_order_book no orderbook",
                        token_id=token_id,
                        hold_seconds=hold_seconds,
                    )
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

            tp_pct = float(pos.get("tp_pct", TP_BASE_PCT))
            sl_pct = float(pos.get("sl_pct", SL_BASE_PCT))
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

            remaining_to_end = float(pos.get("time_left", 0.0) or 0.0) - hold_seconds
            in_endgame = 0 <= remaining_to_end <= float(ENDGAME_GRACE_SEC)

            reason = None
            if best_bid >= tp_target:
                if in_endgame and bool(ENDGAME_ONLY_EMERGENCY_SL):
                    now_ts = time.time()
                    with _positions_lock:
                        cur = open_positions.get(token_id)
                        if cur is None:
                            continue
                        last_notice = float(cur.get("endgame_tp_notified_at", 0.0) or 0.0)
                        should_notice = (now_ts - last_notice) >= float(ENDGAME_NOTIFY_INTERVAL_SEC)
                        if should_notice:
                            cur["endgame_tp_notified_at"] = now_ts
                    if should_notice:
                        record_position_event(
                            "endgame_grace_skip_takeprofit",
                            message="endgame grace active, skipping take profit",
                            token_id=token_id,
                            hold_seconds=hold_seconds,
                            time_left=remaining_to_end,
                            best_bid=best_bid,
                            tp_target=tp_target,
                        )
                        send(
                            f"🕒 末段寬限：暫不止盈\n"
                            f"📋 {pos['question'][:40]}\n"
                            f"現價: {best_bid:.3f} | TP: {tp_target:.3f}\n"
                            f"剩餘: {int(max(remaining_to_end, 0))}s"
                        )
                    continue
                reason = "🎯 達到動態止盈"
            elif best_bid <= sl_target:
                emergency_sl = sl_target * (1 - STOPLOSS_EMERGENCY_EXTRA_PCT)
                if best_bid <= emergency_sl:
                    reason = "🚨 觸發緊急止損"
                    with _positions_lock:
                        if token_id in open_positions:
                            open_positions[token_id]["sl_confirm_count"] = 0
                else:
                    if in_endgame and bool(ENDGAME_ONLY_EMERGENCY_SL):
                        now_ts = time.time()
                        with _positions_lock:
                            cur = open_positions.get(token_id)
                            if cur is None:
                                continue
                            cur["sl_confirm_count"] = 0
                            last_notice = float(cur.get("endgame_grace_notified_at", 0.0) or 0.0)
                            should_notice = (now_ts - last_notice) >= float(ENDGAME_NOTIFY_INTERVAL_SEC)
                            if should_notice:
                                cur["endgame_grace_notified_at"] = now_ts
                        if should_notice:
                            record_position_event(
                                "endgame_grace_skip_stoploss",
                                message="endgame grace active, skipping normal stoploss",
                                token_id=token_id,
                                hold_seconds=hold_seconds,
                                time_left=remaining_to_end,
                                best_bid=best_bid,
                                sl_target=sl_target,
                            )
                            send(
                                f"🕒 末段止損寬限（僅保留緊急止損）\n"
                                f"📋 {pos['question'][:40]}\n"
                                f"現價: {best_bid:.3f} | SL: {sl_target:.3f}\n"
                                f"剩餘: {int(max(remaining_to_end, 0))}s"
                            )
                        continue
                    with _positions_lock:
                        cur = open_positions.get(token_id)
                        if cur is None:
                            continue
                        cur_cnt = int(cur.get("sl_confirm_count", 0)) + 1
                        cur["sl_confirm_count"] = cur_cnt
                    if cur_cnt >= STOPLOSS_CONFIRM_COUNT:
                        reason = "🚨 觸發動態止損(連續確認)"
                    else:
                        if cur_cnt == 1 or cur_cnt % 2 == 0:
                            send(
                                f"⚠️ 止損觀察中 ({cur_cnt}/{STOPLOSS_CONFIRM_COUNT})\n"
                                f"📋 {pos['question'][:40]}\n"
                                f"現價: {best_bid:.3f} | SL: {sl_target:.3f}\n"
                                f"短暫回撤不立即平倉，等待連續確認。"
                            )
                        continue
            elif hold_seconds >= max_hold:
                if in_endgame and bool(ENDGAME_ONLY_EMERGENCY_SL):
                    now_ts = time.time()
                    with _positions_lock:
                        cur = open_positions.get(token_id)
                        if cur is None:
                            continue
                        cur["sl_confirm_count"] = 0
                        last_notice = float(cur.get("endgame_timeout_notified_at", 0.0) or 0.0)
                        should_notice = (now_ts - last_notice) >= float(ENDGAME_NOTIFY_INTERVAL_SEC)
                        if should_notice:
                            cur["endgame_timeout_notified_at"] = now_ts
                    if should_notice:
                        record_position_event(
                            "endgame_grace_skip_timeout",
                            message="endgame grace active, skipping timeout exit",
                            token_id=token_id,
                            hold_seconds=hold_seconds,
                            time_left=remaining_to_end,
                            max_hold=max_hold,
                        )
                        send(
                            f"🕒 末段寬限：暫不因超時平倉\n"
                            f"📋 {pos['question'][:40]}\n"
                            f"持倉: {int(hold_seconds)}s / {int(max_hold)}s\n"
                            f"剩餘: {int(max(remaining_to_end, 0))}s"
                        )
                    continue
                reason = "⏰ 結算規避/超時"
                with _positions_lock:
                    if token_id in open_positions:
                        open_positions[token_id]["sl_confirm_count"] = 0
            else:
                with _positions_lock:
                    if token_id in open_positions:
                        open_positions[token_id]["sl_confirm_count"] = 0

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
