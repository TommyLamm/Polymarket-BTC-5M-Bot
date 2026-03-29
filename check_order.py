import os
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv()
PK = os.getenv("WALLET_PRIVATE_KEY")
FUNDER = os.getenv("FUNDER_ADDRESS")

# 初始化 Client
client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=PK,
    funder=FUNDER,
    signature_type=2,
)

def check_order(order_id):
    try:
        # 向 CLOB API 查詢訂單真實狀態
        order = client.get_order(order_id)
        
        # 轉換為 dictionary 方便讀取
        if hasattr(order, "dict"):
            data = order.dict()
        elif hasattr(order, "__dict__"):
            data = order.__dict__
        else:
            data = order
            
        print(f"\n=== 訂單 {order_id} 狀態報告 ===")
        print(f"狀態 (Status): {data.get('status', 'N/A')}")
        print(f"原下單量 (Original Size): {data.get('original_size', 'N/A')}")
        print(f"引擎判定成交 (Size Matched): {data.get('size_matched', 'N/A')}")
        print(f"手續費 (Fee): {data.get('fee', 'N/A')}")
        
        print("\n👉 如果 Size Matched > 0 但平倉時顯示 balance 0：")
        print("代表 CLOB 撮合引擎雖然配對了你的訂單，但 Polygon 區塊鏈的智能合約結算可能失敗或尚未完成。")
        
    except Exception as e:
         print(f"❌ 查詢訂單失敗: {e}")

if __name__ == "__main__":
    # 這是你在日誌中最後一筆卡住平倉的買單 ID
    check_order("0x8757e5edb5db25838ccb25da1fc219277f6b98ea0694cfaf38f6d634eb7b61f9")
