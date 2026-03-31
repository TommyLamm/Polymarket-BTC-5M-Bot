"""
btc5m.trading — 相容層（對外維持既有匯出）

為避免破壞既有匯入路徑（run_bot.py / telegram_commands.py），
此檔案僅轉發至拆分後模組。
"""

from btc5m.trade_exit import _close_position
from btc5m.position_manager import manage_positions
from btc5m.trade_entry import analyze_and_trade

__all__ = ["_close_position", "manage_positions", "analyze_and_trade"]
