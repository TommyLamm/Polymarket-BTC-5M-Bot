"""
Compatibility shim for legacy module name.
"""

from btc5m.telegram_commands import start_polling, cmd_close_all, cmd_positions, cmd_status

__all__ = ["start_polling", "cmd_close_all", "cmd_positions", "cmd_status"]
