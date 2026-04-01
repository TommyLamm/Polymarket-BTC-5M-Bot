"""
btc5m.config — 環境變數、全局常數、CLOB 客戶端初始化、共享狀態
"""

import os
import threading
import concurrent.futures

import telebot
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

# ======================================================
# ⚙️  環境變數載入
# ======================================================
load_dotenv()


def _require_env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"❌ 缺少必要環境變數: {key}，請檢查 .env 檔案")
    return val


def _parse_signature_type(raw: str) -> int:
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as e:
        raise EnvironmentError("❌ SIGNATURE_TYPE 必須是 0、1 或 2") from e
    if parsed not in (0, 1, 2):
        raise EnvironmentError("❌ SIGNATURE_TYPE 只能是 0、1 或 2")
    return parsed


BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
CHAT_ID = _require_env("TELEGRAM_CHAT_ID")
PRIVATE_KEY = _require_env("WALLET_PRIVATE_KEY")
# FUNDER_ADDRESS：在 polymarket.com/settings 頁面顯示的代理錢包地址
FUNDER_ADDRESS = _require_env("FUNDER_ADDRESS")
# SIGNATURE_TYPE：0=EOA 標準錢包 | 1=POLY_PROXY 魔法連結 | 2=GNOSIS_SAFE（最常見）
SIGNATURE_TYPE = _parse_signature_type(os.getenv("SIGNATURE_TYPE", "2"))

BOT = telebot.TeleBot(BOT_TOKEN)

# ======================================================
# 🎯  目標 Series 設定（已確認，永久有效）
# ======================================================
BTC5M_SERIES_SLUG = "btc-up-or-down-5m"  # Series slug 固定不變
BTC5M_SERIES_ID = 10684                   # 透過 /series API 查詢確認的數字 ID

# ======================================================
# 🔐  Polymarket CLOB 客戶端初始化
# ======================================================
_bootstrap_client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=PRIVATE_KEY,
    funder=FUNDER_ADDRESS,
    signature_type=SIGNATURE_TYPE,
)


def _init_client() -> ClobClient:
    try:
        creds = _bootstrap_client.create_or_derive_api_creds()
        c = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=PRIVATE_KEY,
            creds=creds,
            funder=FUNDER_ADDRESS,
            signature_type=SIGNATURE_TYPE,
        )
        print("✅ Polymarket API 憑證設定成功")
        return c
    except Exception as e:
        raise RuntimeError(f"❌ API 憑證設定失敗，程式終止: {e}")


client = _init_client()

# ======================================================
# 📊  全局交易參數
# ======================================================
MAX_USD           = 10     # 每次最大下單金額 (USDC)
DAILY_MAX_LOSS    = 0.5    # 單日最大損失比例 (50%)
DAILY_TAKE_PROFIT = 0.5    # 單日止盈比例 (50%)
SLIPPAGE          = 0.015   # 最大容許滑點 (1.5%)
ORDER_TIMEOUT     = 10      # 訂單輪詢超時秒數
MIN_SPREAD        = 0.015   # 最小有效買賣價差
MAX_SPREAD        = 0.2     # 最大容許買賣價差
START_CAPITAL     = 75     # 初始資本基準（用於計算每日風控門檻）
ENTRY_COST_TOLERANCE_USD = 0.02  # 成交成本超出 MAX_USD 的容忍值

# ======================================================
# 🛡️  風控與持倉參數
# ======================================================
POS_MAX_HOLD_SEC       = 200   # 最多持倉秒數
MAX_POSITIONS          = 3     # 最多同時持有部位數
COOLDOWN_SEC           = 30    # 同一代幣平倉後冷卻時間
CONSECUTIVE_LOSS_LIMIT = 3     # 連續虧損觸發熔斷的次數
PAUSE_AFTER_LOSS_SEC   = 900   # 熔斷後暫停時間（15 分鐘）
STOPLOSS_CONFIRM_COUNT = 3     # 止損連續確認次數
STOPLOSS_EMERGENCY_EXTRA_PCT = 0.05  # 超過 SL 額外跌幅時啟用緊急止損

# ======================================================
# 🗂️  共享狀態
# ======================================================
_recently_closed: dict   = {}    # {token_id: 平倉時間戳}
_consecutive_losses: int = 0
_pause_until: float      = 0.0

stats_signals_up: int    = 0
stats_signals_down: int  = 0
stats_orders_placed: int = 0

_stats_lock              = threading.Lock()
_pause_until_lock        = threading.Lock()
_consecutive_losses_lock = threading.Lock()
_manage_lock             = threading.Lock()
_analyze_lock            = threading.Lock()
_positions_lock          = threading.Lock()
_send_lock               = threading.Lock()
_balance_lock            = threading.Lock()    # 餘額查詢鎖

open_positions: dict = {}  # {token_id: 持倉資訊 dict}

# 餘額追蹤
_pre_order_balance:  float = -1.0  # 上次下單前餘額
_post_order_balance: float = -1.0  # 上次下單後 10s 餘額

_SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
POSITION_FILE = os.path.join(os.path.dirname(_SCRIPT_DIR), "trades_log.csv")

# 共享執行緒池：避免每次 API 呼叫都建立新池
_API_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)

# 市場資料快取（55 秒 TTL，略小於 5 分鐘窗口）
_market_cache:    list[dict] = []
_market_cache_ts: float      = 0.0
_MARKET_CACHE_TTL            = 55.0
