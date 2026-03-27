"""
btc5m.telegram_cmds — Telegram Bot 指令處理（/close_all, /positions）
"""

import threading

from btc5m.config import BOT, CHAT_ID, FUNDER_ADDRESS, client, open_positions, _positions_lock
from btc5m.trading import _close_position


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
    for token_id in tokens:
        _close_position(token_id)
    BOT.reply_to(message, "✅ 全部清倉完成")


@BOT.message_handler(commands=["positions"])
def cmd_positions(message):
    if str(message.chat.id) != CHAT_ID:
        return
    trades = client.get_trades()
    positions = {}

    # 計算真實持倉
    for t in trades:
        maker = str(t.get("maker") or t.get("maker_address") or "")
        if maker.lower() == FUNDER_ADDRESS.lower():
            tid = str(t["token_id"])
            size = float(t["size"])
            side = t["side"]

            positions.setdefault(tid, 0)
            if side == "BUY":
                positions[tid] += size
            else:
                positions[tid] -= size

    # 過濾掉 0 倉位
    positions = {k: v for k, v in positions.items() if abs(v) > 0.0001}

    if not positions:
        BOT.reply_to(message, "📭 目前沒有任何持倉（真實成交量 = 0）")
        return

    # 整理文字輸出
    msg = "📊 *真實持倉（成交統計）*\n"
    for tid, sz in positions.items():
        msg += f"• Token `{tid[:10]}…` 份數: `{sz:.4f}`\n"

    BOT.reply_to(message, msg, parse_mode="Markdown")


# ======================================================
# 🚀  啟動背景監聽
# ======================================================

def start_polling():
    """在背景執行緒啟動 Telegram Bot polling。"""
    threading.Thread(
        target=lambda: BOT.infinity_polling(timeout=20),
        daemon=True
    ).start()
