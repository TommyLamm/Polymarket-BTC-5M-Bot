import os
import json
from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3

# 載入環境變數
load_dotenv()

PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS")
SIGNATURE_TYPE = os.getenv("SIGNATURE_TYPE", "2")

# 嘗試多個 Polygon RPC
RPC_URLS = [
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
    "https://polygon-rpc.com",
    "https://rpc-mainnet.maticvigil.com"
]

w3 = None
for rpc in RPC_URLS:
    temp_w3 = Web3(Web3.HTTPProvider(rpc))
    if temp_w3.is_connected():
        w3 = temp_w3
        # print(f"🔗 成功連接到 RPC: {rpc}")
        break

if not w3 or not w3.is_connected():
    print("❌ 所有 Polygon RPC 都無法連線，請稍後再試或檢查網路")
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# ERC20 ABI (只需要 balanceOf)
ERC20_ABI = json.loads('''[
    {"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}
]''')

def check_wallet():
    print("\n=== Polymarket Bot 餘額與設定檢查 ===\n")
    
    if not PRIVATE_KEY:
        print("❌ 找不到 WALLET_PRIVATE_KEY")
        return
        
    try:
        eoa_account = Account.from_key(PRIVATE_KEY)
        eoa_address = eoa_account.address
        print(f"🔑 EOA 錢包地址 (從 Private Key 推導): {eoa_address}")
    except Exception as e:
        print(f"❌ 私鑰解析失敗: {e}")
        return

    print(f"🏦 指定的 FUNDER_ADDRESS (代理錢包/目標地址): {FUNDER_ADDRESS}")
    print(f"📝 目前的 SIGNATURE_TYPE: {SIGNATURE_TYPE}")
    rpc_ok = bool(w3 and w3.is_connected())
    print(f"🌐 Polygon RPC 連線狀態: {'🟢 成功' if rpc_ok else '🔴 失敗'}\n")

    if not rpc_ok:
        return

    usdc_contract = w3.eth.contract(address=w3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
    
    addresses_to_check = {
        "EOA 地址 (Private Key)": eoa_address,
        "Funder 地址 (.env)": FUNDER_ADDRESS
    }

    print("--- USDC.e (Polygon) 餘額檢查 ---")
    for name, addr in addresses_to_check.items():
        if not addr:
            continue
            
        try:
            checksum_addr = w3.to_checksum_address(addr)
            # 取得 MATIC 餘額
            matic_bal = w3.eth.get_balance(checksum_addr) / 10**18
            # 取得 USDC 餘額
            usdc_bal = usdc_contract.functions.balanceOf(checksum_addr).call() / 10**6
            
            print(f"\n[{name}] {checksum_addr}")
            print(f"  USDC.e 餘額: {usdc_bal:.4f} USDC")
            print(f"  MATIC 餘額: {matic_bal:.4f} MATIC")
            
        except Exception as e:
            print(f"❌ 查詢 {name} 失敗: {e}")

    print("\n------------------------------------\n分析結果:")
    print("1. 剛才看到的報錯 order amount: 9355400 代表約 9.35 USDC (6位小數)")
    print("2. 報錯表示 balance: 0，如果你在上面的 Funder 地址看到的餘額是 0，代表代理錢包沒錢")
    print("3. 如果 EOA 有錢，而 Funder 沒錢，請進入 Polymarket 網頁把錢轉到代理錢包 (Deposit / Relayer)")
    print("4. 如果 EOA 也是代理錢包 (也就是 SIGNATURE_TYPE=0)，那麼 .env 的 FUNDER_ADDRESS 必須等於 EOA 地址\n")

if __name__ == "__main__":
    check_wallet()
