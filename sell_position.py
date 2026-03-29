"""
sell_position.py — 測試賣出/領取持倉工具

功能：
  1. 列出目前所有鏈上持倉
  2. 嘗試賣出活躍市場的持倉
  3. 對已結算市場，嘗試 redeem（領取獎金）
"""

import os
import sys
import json
import time

from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY, SELL

load_dotenv()

PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "2"))

# ── 初始化客戶端 ──────────────────────────────
print("🔧 初始化 Polymarket CLOB 客戶端...")
bootstrap = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=PRIVATE_KEY,
    funder=FUNDER_ADDRESS,
    signature_type=SIGNATURE_TYPE,
)
creds = bootstrap.create_or_derive_api_creds()
client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=PRIVATE_KEY,
    creds=creds,
    funder=FUNDER_ADDRESS,
    signature_type=SIGNATURE_TYPE,
)
print("✅ 客戶端初始化成功\n")


def parse_orderbook(book):
    """解析訂單簿"""
    bids = getattr(book, "bids", []) if hasattr(book, "bids") else book.get("bids", [])
    asks = getattr(book, "asks", []) if hasattr(book, "asks") else book.get("asks", [])
    if not bids or not asks:
        return None, None
    def _p(item):
        return float(item.price) if hasattr(item, "price") else float(item["price"])
    return max(_p(b) for b in bids), min(_p(a) for a in asks)


def get_all_positions():
    """透過 get_trades 取得鏈上持倉"""
    print("📊 查詢鏈上交易記錄...")
    try:
        trades = client.get_trades()
        print(f"   找到 {len(trades)} 筆交易記錄")
    except Exception as e:
        print(f"❌ get_trades 失敗: {e}")
        return {}

    positions = {}
    funder_lower = FUNDER_ADDRESS.lower()
    for t in trades:
        maker = str(t.get("maker") or t.get("maker_address") or "").lower()
        taker = str(t.get("taker") or t.get("taker_address") or "").lower()
        if maker != funder_lower and taker != funder_lower:
            continue

        tid = str(t["token_id"])
        sz = float(t["size"])
        side = t.get("side", "")
        price = float(t.get("price", 0))

        if tid not in positions:
            positions[tid] = {"net_size": 0, "trades": [], "avg_entry": 0, "total_cost": 0}

        positions[tid]["trades"].append(t)
        if side == "BUY":
            positions[tid]["total_cost"] += sz * price
            positions[tid]["net_size"] += sz
        else:
            positions[tid]["net_size"] -= sz

    # 計算平均進場價
    for tid, p in positions.items():
        if p["net_size"] > 0.001 and p["total_cost"] > 0:
            p["avg_entry"] = p["total_cost"] / p["net_size"]

    # 只保留有淨持倉的
    return {k: v for k, v in positions.items() if abs(v["net_size"]) > 0.001}


def try_sell(token_id: str, size: float):
    """嘗試以市價賣出"""
    print(f"\n📤 嘗試賣出 token={token_id[:16]}… size={size:.4f}")

    # 查訂單簿
    try:
        book = client.get_order_book(token_id)
        best_bid, best_ask = parse_orderbook(book)
        print(f"   📖 訂單簿: bid={best_bid} ask={best_ask}")
    except Exception as e:
        print(f"   ❌ 訂單簿查詢失敗: {e}")
        print(f"   → 市場可能已結算，請到 Polymarket 網頁領取（Redeem）")
        return False

    if best_bid is None:
        print(f"   ⚠️ 訂單簿為空，市場可能已結算")
        print(f"   → 請到 Polymarket 網頁領取（Redeem）")
        return False

    # 市價賣出：掛在 best_bid 下方確保立即成交
    limit_price = round(max(best_bid - 0.01, 0.01), 3)
    safe_size = round(size * 0.99, 2)
    if safe_size < 0.01:
        safe_size = round(size, 2)

    print(f"   💰 賣出價: {limit_price} (bid={best_bid})")
    print(f"   📦 賣出量: {safe_size}")

    try:
        order_args = OrderArgs(
            price=limit_price,
            size=safe_size,
            side=SELL,
            token_id=token_id,
        )
        signed = client.create_order(order_args)
        resp = client.post_order(signed)

        success = (getattr(resp, "success", False)
                   or (isinstance(resp, dict) and resp.get("success")))
        if success:
            oid = None
            if hasattr(resp, "orderID") and resp.orderID:
                oid = resp.orderID
            elif isinstance(resp, dict):
                oid = resp.get("orderID") or resp.get("id")

            print(f"   ✅ 賣出訂單已送出! ID: {oid}")

            # 輪詢成交
            if oid:
                for _ in range(10):
                    try:
                        st = client.get_order(oid)
                        status = (getattr(st, "status", "") if hasattr(st, "status")
                                  else st.get("status", ""))
                        size_matched = 0.0
                        if hasattr(st, "size_matched"):
                            size_matched = float(st.size_matched or 0)
                        elif isinstance(st, dict):
                            size_matched = float(st.get("size_matched") or 0)
                        print(f"   🔍 狀態: {status} | 成交: {size_matched}")
                        if status in ("MATCHED", "ORDER_STATUS_MATCHED",
                                      "FILLED", "ORDER_STATUS_FILLED",
                                      "PARTIALLY_MATCHED", "PARTIALLY_FILLED"):
                            print(f"   🎉 賣出成交！數量: {size_matched}")
                            return True
                    except Exception as e:
                        print(f"   🔍 查詢異常: {e}")
                    time.sleep(2)
                print(f"   ⏰ 輪詢超時，請手動確認")
            return True
        else:
            err = (getattr(resp, "errorMsg", "")
                   or (resp.get("errorMsg", "") if isinstance(resp, dict) else ""))
            print(f"   ❌ 賣出失敗: {err}")
            print(f"   完整回應: {resp}")
            return False
    except Exception as e:
        print(f"   ❌ 賣出異常: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    print("=" * 50)
    print("  Polymarket 持倉賣出/領取工具")
    print("=" * 50)

    positions = get_all_positions()

    if not positions:
        print("\n📭 沒有找到任何鏈上持倉")
        return

    print(f"\n📋 找到 {len(positions)} 個持倉:\n")
    for i, (tid, p) in enumerate(positions.items()):
        print(f"  [{i+1}] Token: {tid[:20]}…")
        print(f"      淨持倉: {p['net_size']:.4f} 份")
        print(f"      平均進場: {p['avg_entry']:.4f}")
        print(f"      交易數: {len(p['trades'])} 筆")

        # 嘗試查看訂單簿
        try:
            book = client.get_order_book(tid)
            best_bid, best_ask = parse_orderbook(book)
            if best_bid:
                unrealized = (best_bid - p['avg_entry']) * p['net_size']
                print(f"      現價: bid={best_bid} ask={best_ask}")
                print(f"      浮動 PnL: {unrealized:+.4f} USDC")
                print(f"      狀態: ✅ 市場活躍")
            else:
                print(f"      狀態: 🏁 市場可能已結算（無流動性）")
        except Exception:
            print(f"      狀態: 🏁 市場可能已結算（查詢失敗）")
        print()

    # 互動選擇
    choice = input("🔧 操作:\n"
                   "  [a] 嘗試賣出所有持倉\n"
                   "  [1-N] 賣出指定持倉\n"
                   "  [q] 退出\n"
                   ">>> ").strip().lower()

    if choice == 'q':
        return

    token_ids = list(positions.keys())

    if choice == 'a':
        for tid in token_ids:
            try_sell(tid, positions[tid]["net_size"])
    else:
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(token_ids):
                tid = token_ids[idx]
                try_sell(tid, positions[tid]["net_size"])
            else:
                print("❌ 無效選擇")
        except ValueError:
            print("❌ 無效輸入")


if __name__ == "__main__":
    main()
