"""
btc5m.telegram_commands — Telegram Bot 指令處理（/close_all, /positions, /status）
"""

import time
import datetime
import threading

from btc5m.config import (
    BOT, CHAT_ID, FUNDER_ADDRESS, client,
    open_positions, _positions_lock,
    MAX_POSITIONS, POS_MAX_HOLD_SEC, START_CAPITAL,
)
from btc5m.utils import (
    _api_call_with_timeout,
    _parse_orderbook,
    get_daily_realized_pnl,
    get_usdc_balance,
    extract_list_payload,
)
from btc5m.trading import _close_position
from btc5m.observability import record_position_event, summarize_missed_trades


# ======================================================
# 📱  Telegram 指令
# ======================================================

@BOT.message_handler(commands=['close_all'])
def cmd_close_all(message):
    if str(message.chat.id) != CHAT_ID:
        return
    with _positions_lock:
        tokens = list(open_positions.keys())
    if not tokens:
        BOT.reply_to(message, "📭 目前沒有持倉")
        return
    BOT.reply_to(message, f"🔄 開始清倉 {len(tokens)} 個部位...")
    outcomes = []
    for token_id in tokens:
        _close_position(token_id)
        with _positions_lock:
            cur = open_positions.get(token_id)
        if cur is None:
            outcomes.append((token_id, "closed"))
            continue
        if cur.get("exit_state") == "dust_pending_settlement":
            outcomes.append((token_id, "dust_pending"))
        else:
            outcomes.append((token_id, "still_open"))

    closed = sum(1 for _, s in outcomes if s == "closed")
    dust = sum(1 for _, s in outcomes if s == "dust_pending")
    still_open = sum(1 for _, s in outcomes if s == "still_open")

    if dust > 0 or still_open > 0:
        lines = [
            "⚠️ 清倉部分完成",
            f"已平倉: {closed}",
            f"dust待結算: {dust}",
            f"仍未平倉: {still_open}",
        ]
        for tid, status in outcomes:
            status_label = (
                "✅ 已平倉" if status == "closed"
                else "🟡 dust待結算" if status == "dust_pending"
                else "❌ 仍未平倉"
            )
            lines.append(f"• {tid[:12]}… {status_label}")
        BOT.reply_to(message, "\n".join(lines))
        record_position_event(
            "close_all_partial",
            message="close_all completed with unresolved positions",
            closed=closed,
            dust_pending=dust,
            still_open=still_open,
        )
    else:
        BOT.reply_to(message, f"✅ 全部清倉完成（{closed}/{len(tokens)}）")


@BOT.message_handler(commands=["positions"])
def cmd_positions(message):
    if str(message.chat.id) != CHAT_ID:
        return

    # 先顯示 Bot 內部追蹤的活躍持倉（包含即時浮動損益）
    with _positions_lock:
        active = dict(open_positions)

    now_dt = datetime.datetime.now(datetime.timezone.utc)

    if active:
        msg = f"📊 *活躍持倉 ({len(active)}/{MAX_POSITIONS})*\n{'─'*28}\n"
        total_upnl = 0.0
        for tid, pos in active.items():
            hold_sec = int((now_dt - pos["opened_at"]).total_seconds())
            # 嘗試取得即時市價
            try:
                book = _api_call_with_timeout(client.get_order_book, tid)
                best_bid, _ = _parse_orderbook(book)
            except Exception as e:
                print(f"⚠️ 查詢持倉訂單簿失敗: {e} | token={tid[:12]}…")
                best_bid = None

            entry = pos["entry_price"]
            size  = pos["size"]

            if best_bid is not None:
                upnl = (best_bid - entry) * size
                upnl_pct = (best_bid - entry) / entry * 100
                total_upnl += upnl
                price_str = f"現價: `{best_bid:.3f}`"
                pnl_str = f"浮動: `{upnl:+.4f}` USDC (`{upnl_pct:+.1f}%`)"
            else:
                price_str = "現價: `N/A`"
                pnl_str = "浮動: `N/A`"

            msg += (
                f"• `{pos.get('question', 'N/A')[:35]}`\n"
                f"  進場: `{entry:.3f}` | {price_str}\n"
                f"  數量: `{size:.2f}` 份 | 持倉: `{hold_sec}s`\n"
                f"  {pnl_str}\n"
                f"  TP: `{pos['tp_pct']*100:.0f}%` / SL: `{pos['sl_pct']*100:.0f}%`\n\n"
            )
        msg += f"{'─'*28}\n💰 總浮動 PnL: `{total_upnl:+.4f}` USDC"
    else:
        msg = "📭 目前沒有活躍持倉"

    # 再顯示鏈上成交統計
    try:
        trades_resp = _api_call_with_timeout(client.get_trades)
        trades = extract_list_payload(trades_resp, keys=("data", "results", "items", "trades"))
        chain_positions = {}
        for t in trades:
            if not isinstance(t, dict):
                continue
            # 檢查 maker 和 taker 兩邊（吃單方=taker）
            maker = str(t.get("maker") or t.get("maker_address") or "").lower()
            taker = str(t.get("taker") or t.get("taker_address") or "").lower()
            funder = FUNDER_ADDRESS.lower()
            if maker == funder or taker == funder:
                raw_tid = t.get("token_id") or t.get("asset_id") or t.get("tokenId")
                if not raw_tid:
                    continue
                tid_str = str(raw_tid)
                sz = float(t["size"])
                side = t.get("side", "")
                chain_positions.setdefault(tid_str, 0)
                if side == "BUY":
                    chain_positions[tid_str] += sz
                else:
                    chain_positions[tid_str] -= sz
        chain_positions = {k: v for k, v in chain_positions.items() if abs(v) > 0.0001}
        if chain_positions:
            msg += f"\n\n📈 *鏈上成交統計*\n"
            for tid_str, sz in chain_positions.items():
                msg += f"• `{tid_str[:12]}…` → `{sz:.4f}` 份\n"
    except Exception as e:
        print(f"⚠️ 讀取鏈上成交統計失敗: {e}")

    BOT.reply_to(message, msg, parse_mode="Markdown")


@BOT.message_handler(commands=["status"])
def cmd_status(message):
    if str(message.chat.id) != CHAT_ID:
        return
    pnl_today = get_daily_realized_pnl()
    with _positions_lock:
        pos_count = len(open_positions)

    import btc5m.config as cfg
    now = time.time()
    with cfg._pause_until_lock:
        pause = cfg._pause_until
    paused = now < pause

    with cfg._stats_lock:
        s_up = cfg.stats_signals_up
        s_dn = cfg.stats_signals_down
        s_ord = cfg.stats_orders_placed

    # 即時查詢餘額
    live_bal = get_usdc_balance()
    live_bal_str = f"`{live_bal:.4f}` USDC" if live_bal >= 0 else "`查詢失敗`"

    # 上次下單前後餘額
    with cfg._balance_lock:
        pre_b  = cfg._pre_order_balance
        post_b = cfg._post_order_balance

    if pre_b >= 0:
        pre_bal_str = f"`{pre_b:.4f}` USDC"
    else:
        pre_bal_str = "`尚未記錄`"

    if post_b >= 0:
        delta = post_b - pre_b if pre_b >= 0 else 0
        post_bal_str = f"`{post_b:.4f}` USDC (`{delta:+.4f}`)"
    else:
        post_bal_str = "`尚未記錄`"

    msg = (
        f"🤖 *Bot 狀態報告*\n"
        f"{'\u2500'*28}\n"
        f"⏰ {datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')} HKT\n"
        f"📊 持倉: `{pos_count}/{MAX_POSITIONS}`\n"
        f"💰 即時餘額: {live_bal_str}\n"
        f"📈 今日 PnL: `{pnl_today:+.4f}` USDC\n"
        f"📊 資本基準: `{cfg.START_CAPITAL:.4f}` USDC\n"
        f"🛡️ 熔斷狀態: `{'\u274c 暫停中 (剩餘 ' + str(int(pause - now)) + 's)' if paused else '\u2705 正常運行'}`\n"
        f"📡 捕獲信號: 🐂 `{s_up}` 次 / 🐻 `{s_dn}` 次\n"
        f"🎯 有效下單: `{s_ord}` 次\n"
        f"{'\u2500'*28}\n"
        f"💳 上次下單前餘額: {pre_bal_str}\n"
        f"💳 下單後餘額(10s): {post_bal_str}\n"
        f"{'\u2500'*28}"
    )
    BOT.reply_to(message, msg, parse_mode="Markdown")


# ======================================================
# 🚀  啟動背景監聯
# ======================================================

def start_polling():
    """在背景執行緒啟動 Telegram Bot polling。"""
    threading.Thread(
        target=lambda: BOT.infinity_polling(timeout=20),
        daemon=True
    ).start()
