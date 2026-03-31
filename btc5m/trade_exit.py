"""
btc5m.trade_exit — 平倉執行邏輯
"""

import time
import datetime
import traceback

import btc5m.config as cfg
from btc5m.config import (
    client,
    CONSECUTIVE_LOSS_LIMIT, PAUSE_AFTER_LOSS_SEC,
    open_positions,
    _positions_lock, _pause_until_lock, _consecutive_losses_lock,
)
from btc5m.utils import (
    send, _api_call_with_timeout, log_trade,
    get_daily_realized_pnl, _parse_orderbook, fetch_live_positions,
    get_conditional_token_balance,
    _get_order_id,
)
from btc5m.order_execution_utils import (
    _extract_orderbook_constraints,
    _quantize_down,
    _cancel_order_and_validate,
    _poll_order_matched,
)
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import SELL


def _notify_position_pending_redeem(token_id: str, pos: dict, hold_seconds: float, reason: str = ""):
    reason_text = f"\n原因: {reason}" if reason else ""
    send(
        f"🏁 倉位已進入結算/待領取\n"
        f"{'─'*28}\n"
        f"📋 {pos.get('question', 'N/A')[:40]}\n"
        f"Token: {token_id[:12]}…\n"
        f"持倉時間: {int(hold_seconds)}s\n"
        f"已自本地持倉清除，請至 Polymarket 頁面 Redeem/Claim。"
        f"{reason_text}\n"
        f"{'─'*28}"
    )


def _reconcile_position_after_exit_attempt(token_id: str, pos: dict) -> bool:
    """
    依 Data API 真實持倉對賬：
    - 若 token 已無持倉，視為已關閉，清除本地追蹤。
    - 若標記 redeemable，視為結算待領取，清除本地追蹤並通知。
    回傳 True 代表已完成清理，False 代表仍需保留並重試平倉。
    """
    live_positions = fetch_live_positions()
    if live_positions is None:
        return False

    row = live_positions.get(token_id)
    live_size = float(row.get("size", 0.0) or 0.0) if row else 0.0
    token_balance = get_conditional_token_balance(token_id)
    effective_size = live_size
    if token_balance >= 0:
        effective_size = max(live_size, token_balance)

    if row is None and effective_size <= 0.0001:
        with _positions_lock:
            open_positions.pop(token_id, None)
        hold_seconds = (
            datetime.datetime.now(datetime.timezone.utc) - pos["opened_at"]
        ).total_seconds()
        send(
            f"✅ 對賬確認：鏈上已無該持倉\n"
            f"Token: {token_id[:12]}… | 持倉時間: {int(hold_seconds)}s"
        )
        return True

    if bool(row.get("redeemable", False)):
        with _positions_lock:
            open_positions.pop(token_id, None)
        hold_seconds = (
            datetime.datetime.now(datetime.timezone.utc) - pos["opened_at"]
        ).total_seconds()
        _notify_position_pending_redeem(
            token_id,
            pos,
            hold_seconds,
            reason="Data API 回傳 redeemable=true",
        )
        return True

    if effective_size >= 0:
        with _positions_lock:
            if token_id in open_positions:
                open_positions[token_id]["size"] = max(effective_size, 0.0)

    return False


def _sync_local_position_size(token_id: str, local_pos: dict, live_row: dict | None = None):
    """
    用鏈上可用 token 餘額同步本地 size，避免因部分成交/結算造成幽靈倉位。
    """
    live_size = None
    if live_row is not None:
        live_size = float(live_row.get("size", 0.0) or 0.0)
    if live_size is None:
        live_size = 0.0

    token_balance = get_conditional_token_balance(token_id)
    if token_balance < 0:
        return

    synced_size = min(token_balance, live_size if live_size > 0 else token_balance)
    with _positions_lock:
        if token_id in open_positions:
            open_positions[token_id]["size"] = max(synced_size, 0.0)
            if synced_size <= 0.0001:
                open_positions.pop(token_id, None)
                hold_seconds = (
                    datetime.datetime.now(datetime.timezone.utc) - local_pos["opened_at"]
                ).total_seconds()
                send(
                    f"✅ 對賬同步：可用餘額為 0，移除本地持倉\n"
                    f"Token: {token_id[:12]}… | 持倉時間: {int(hold_seconds)}s"
                )


def _close_position(token_id: str, reason: str = None, tp_target: float = None, sl_target: float = None):
    """以市價賣出平倉。5 分鐘二元市場必須果斷出場，不做任何限價保護。"""
    with _positions_lock:
        if token_id not in open_positions:
            return
        pos = open_positions[token_id].copy()
        size, entry_price = pos["size"], pos["entry_price"]
        pending_oid = pos.get("pending_sell_oid")

    if pending_oid:
        canceled = _cancel_order_and_validate(pending_oid, "清理殘留掛單")
        if not canceled:
            send(f"⚠️ 殘留掛單取消失敗，暫停本次平倉重試\n"
                 f"Token: {token_id[:12]}…\n"
                 f"掛單: {pending_oid[:16]}…")
            _reconcile_position_after_exit_attempt(token_id, pos)
            return
        with _positions_lock:
            if token_id in open_positions:
                open_positions[token_id].pop("pending_sell_oid", None)
        time.sleep(1)

    try:
        book = _api_call_with_timeout(client.get_order_book, token_id)
        best_bid, best_ask = _parse_orderbook(book)
        if best_bid is None or best_ask is None:
            send(f"⚠️ 平倉失敗：訂單簿無流動性\n"
                 f"Token: {token_id[:12]}…\n"
                 f"持倉: {size:.2f} 份 @ {entry_price:.3f}")
            _reconcile_position_after_exit_attempt(token_id, pos)
            return

        tick_size, min_order_size = _extract_orderbook_constraints(book)
        target_price = max(best_bid - tick_size, tick_size)
        limit_price = _quantize_down(target_price, tick_size, tick_size)

        safe_size = _quantize_down(size, 0.01, 0.0)
        if safe_size + 1e-9 < min_order_size:
            send(f"⚠️ 平倉失敗：可賣數量無效\nToken: {token_id[:12]}…\n持倉: {size:.4f}")
            live_positions = fetch_live_positions()
            live_row = live_positions.get(token_id) if isinstance(live_positions, dict) else None
            _sync_local_position_size(token_id, pos, live_row)
            _reconcile_position_after_exit_attempt(token_id, pos)
            return
        safe_size = min(safe_size, size)

        print(
            f"📤 平倉下單: price={limit_price} size={safe_size} "
            f"bid={best_bid} ask={best_ask} tick={tick_size} min_size={min_order_size}"
        )

        order_args = OrderArgs(price=limit_price, size=safe_size, side=SELL, token_id=token_id)
        signed_order = _api_call_with_timeout(client.create_order, order_args)
        resp = _api_call_with_timeout(client.post_order, signed_order, OrderType.FAK)

        success = (getattr(resp, "success", False)
                   or (isinstance(resp, dict) and resp.get("success")))
        if not success:
            err = (getattr(resp, "errorMsg", "")
                   or (resp.get("errorMsg", "") if isinstance(resp, dict) else ""))
            send(f"⚠️ 平倉下單失敗\n"
                 f"Token: {token_id[:12]}…\n"
                 f"持倉: {size:.2f} 份 @ {entry_price:.3f}\n"
                 f"賣出價: {limit_price} | bid: {best_bid} | ask: {best_ask}\n"
                 f"錯誤: {err}")
            if "NOT_ENOUGH_BALANCE" in err.upper() or "not enough balance" in err.lower():
                live_positions = fetch_live_positions()
                live_row = live_positions.get(token_id) if isinstance(live_positions, dict) else None
                _sync_local_position_size(token_id, pos, live_row)
            _reconcile_position_after_exit_attempt(token_id, pos)
            return

        oid = _get_order_id(resp)
        exit_price = limit_price
        size_matched = 0.0

        if oid:
            filled, exit_price, size_matched = _poll_order_matched(
                oid, limit_price, fail_on_unmatched=True
            )
            if not filled:
                send(f"⚠️ 平倉訂單超時未成交，嘗試取消\n"
                     f"訂單 ID: {oid[:16]}…\n"
                     f"嘗試賣出: {safe_size:.2f} 份 @ {limit_price:.3f}")
                canceled = _cancel_order_and_validate(oid, "平倉超時")
                if not canceled:
                    with _positions_lock:
                        if token_id in open_positions:
                            open_positions[token_id]["pending_sell_oid"] = oid
                _reconcile_position_after_exit_attempt(token_id, pos)
                return

            if size_matched > 0 and size_matched + 1e-6 < safe_size:
                canceled = _cancel_order_and_validate(oid, "部分成交後清理殘單")
                if not canceled:
                    with _positions_lock:
                        if token_id in open_positions:
                            open_positions[token_id]["pending_sell_oid"] = oid
        else:
            send("⚠️ 無法取得平倉訂單 ID，以限價估算退出價格")

        effective_exit_size = safe_size
        if size_matched > 0:
            effective_exit_size = min(size, size_matched)
        if effective_exit_size <= 0:
            effective_exit_size = min(size, safe_size)

        slippage_pct = round((exit_price - best_bid) / best_bid, 6) if best_bid else 0
        fee_rate = 0.0156
        entry_fee = entry_price * effective_exit_size * fee_rate
        exit_fee = exit_price * effective_exit_size * fee_rate
        total_fee = entry_fee + exit_fee

        realized_pnl = (exit_price - entry_price) * effective_exit_size - total_fee
        remaining_size = max(size - effective_exit_size, 0.0)
        fully_closed = remaining_size <= 0.01

        log_trade({
            "date": datetime.date.today().isoformat(),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "token_id": token_id,
            "side": "sell",
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "size": round(effective_exit_size, 4),
            "slippage_pct": slippage_pct,
            "realized_pnl": round(realized_pnl, 4),
            "fees": round(total_fee, 4),
            "status": "closed" if fully_closed else "partially_closed",
            "hold_time": (datetime.datetime.now(datetime.timezone.utc) - pos["opened_at"]).total_seconds()
        })
        hold_time = (datetime.datetime.now(datetime.timezone.utc) - pos["opened_at"]).total_seconds()
        pnl_pct = (realized_pnl / (entry_price * effective_exit_size) * 100) if (entry_price * effective_exit_size) else 0
        pnl_emoji = "🟢" if realized_pnl >= 0 else "🔴"
        if fully_closed:
            send(f"📤 平倉完成\n"
                 f"{'─'*28}\n"
                 f"📋 {pos.get('question', 'N/A')[:40]}\n"
                 f"Token: {token_id[:12]}…\n"
                 f"進場: {entry_price:.3f} → 出場: {exit_price:.3f}\n"
                 f"數量: {effective_exit_size:.4f} 份 | 持倉: {int(hold_time)}s\n"
                 f"滑點: {slippage_pct*100:.3f}%\n"
                 f"{pnl_emoji} PnL: {realized_pnl:+.4f} USDC ({pnl_pct:+.2f}%)\n"
                 f"{'─'*28}")
            with _positions_lock:
                open_positions.pop(token_id, None)
        else:
            send(f"⚠️ 部分平倉完成\n"
                 f"{'─'*28}\n"
                 f"📋 {pos.get('question', 'N/A')[:40]}\n"
                 f"Token: {token_id[:12]}…\n"
                 f"已賣: {effective_exit_size:.4f} 份 @ {exit_price:.3f}\n"
                 f"剩餘: {remaining_size:.4f} 份\n"
                 f"{pnl_emoji} 已實現 PnL: {realized_pnl:+.4f} USDC\n"
                 f"{'─'*28}")
            with _positions_lock:
                if token_id in open_positions:
                    open_positions[token_id]["size"] = remaining_size
                    open_positions[token_id].pop("pending_sell_oid", None)

        if fully_closed:
            with _consecutive_losses_lock:
                if realized_pnl < 0:
                    cfg._consecutive_losses += 1
                    if cfg._consecutive_losses >= CONSECUTIVE_LOSS_LIMIT:
                        with _pause_until_lock:
                            cfg._pause_until = time.time() + PAUSE_AFTER_LOSS_SEC
                        pnl_today = get_daily_realized_pnl()
                        send(f"🛡️ 熔斷觸發！\n"
                             f"{'─'*28}\n"
                             f"連續虧損: {cfg._consecutive_losses} 次\n"
                             f"暫停時間: {PAUSE_AFTER_LOSS_SEC // 60} 分鐘\n"
                             f"今日累計 PnL: {pnl_today:+.4f} USDC\n"
                             f"預計恢復: {datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%H:%M:%S')} + {PAUSE_AFTER_LOSS_SEC // 60}min\n"
                             f"{'─'*28}")
                        cfg._consecutive_losses = 0
                else:
                    cfg._consecutive_losses = 0

    except Exception as e:
        err_str = str(e)
        hold_time = (datetime.datetime.now(datetime.timezone.utc)
                     - pos["opened_at"]).total_seconds()

        if "not enough balance" in err_str:
            live_positions = fetch_live_positions()
            live_row = live_positions.get(token_id) if isinstance(live_positions, dict) else None
            _sync_local_position_size(token_id, pos, live_row)
            with _positions_lock:
                already_warned = open_positions.get(token_id, {}).get("phantom_warned", False)
            if hold_time > 60 and not already_warned:
                with _positions_lock:
                    if token_id in open_positions:
                        open_positions[token_id]["phantom_warned"] = True
                send(f"👻 警告：持倉已 {int(hold_time)} 秒仍無餘額！\n"
                     f"{'─'*28}\n"
                     f"📋 {pos.get('question', 'N/A')[:40]}\n"
                     f"Token: {token_id[:12]}…\n"
                     f"可能遭遇 Polygon 網路嚴重擁塞，或結算已徹底失敗。\n"
                     f"Bot 會繼續為您監測，直到市場結束。\n"
                     f"{'─'*28}")
            else:
                print(f"⏳ 等待鏈上結算到帳... ({int(hold_time)}s)")
        else:
            send(f"❌ 平倉異常: {e}\nToken: {token_id[:12]}…")
            traceback.print_exc()

        _reconcile_position_after_exit_attempt(token_id, pos)
