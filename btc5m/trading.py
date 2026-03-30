"""
btc5m.trading — 交易執行與倉位管理（買入、平倉、持倉監控、主循環）
"""

import time
import datetime
import traceback

from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY, SELL

import btc5m.config as cfg
from btc5m.config import (
    client,
    MAX_USD, SLIPPAGE, ORDER_TIMEOUT, MIN_SPREAD, MAX_SPREAD,
    START_CAPITAL, DAILY_MAX_LOSS, DAILY_TAKE_PROFIT,
    POS_MAX_HOLD_SEC, MAX_POSITIONS, COOLDOWN_SEC,
    CONSECUTIVE_LOSS_LIMIT, PAUSE_AFTER_LOSS_SEC,
    open_positions, _recently_closed,
    _positions_lock, _manage_lock, _analyze_lock,
    _pause_until_lock, _consecutive_losses_lock,
)
from btc5m.utils import (
    send, _api_call_with_timeout, log_trade,
    get_daily_realized_pnl, _parse_orderbook,
    _get_order_id, _clean_recently_closed, get_usdc_balance,
)
from btc5m.market import fetch_active_btc5m_markets, _resolve_token_id
from btc5m.signals import get_btc_signals


# ======================================================
# 🔄  訂單成交輪詢
# ======================================================

def _poll_order_matched(oid: str, fallback_price: float) -> tuple:
    """
    輪詢訂單狀態直到成交或超時。
    回傳 (成交成功: bool, 成交估算價: float, 實際成交量: float)
    """
    t0 = time.time()
    while time.time() - t0 < ORDER_TIMEOUT:
        try:
            st = _api_call_with_timeout(client.get_order, oid)
            status = (getattr(st, "status", "") if hasattr(st, "status")
                      else st.get("status", ""))

            # 提取 size_matched（實際成交量）
            size_matched = 0.0
            if hasattr(st, "size_matched"):
                size_matched = float(st.size_matched or 0)
            elif isinstance(st, dict):
                size_matched = float(st.get("size_matched") or 0)

            # 嘗試取得成交均價
            avg_price = fallback_price
            if hasattr(st, "price") and st.price:
                try:
                    avg_price = float(st.price)
                except (ValueError, TypeError):
                    pass
            elif isinstance(st, dict) and st.get("price"):
                try:
                    avg_price = float(st["price"])
                except (ValueError, TypeError):
                    pass

            print(f"🔍 訂單狀態: {status} | size_matched: {size_matched}")

            if status in ("MATCHED", "ORDER_STATUS_MATCHED",
                          "FILLED", "ORDER_STATUS_FILLED"):
                return True, avg_price, size_matched
            if status in ("PARTIALLY_MATCHED", "ORDER_STATUS_PARTIALLY_FILLED",
                          "PARTIALLY_FILLED"):
                return True, avg_price, size_matched
        except Exception as e:
            print(f"🔍 輪詢異常: {e}")
        time.sleep(2)
    return False, fallback_price, 0.0


# ======================================================
# 📤  平倉邏輯
# ======================================================

def _close_position(token_id: str, reason: str = None, tp_target: float = None, sl_target: float = None):
    """以市價賣出平倉。5 分鐘二元市場必須果斷出場，不做任何限價保護。"""
    with _positions_lock:
        if token_id not in open_positions:
            return
        pos = open_positions[token_id].copy()
        size, entry_price = pos["size"], pos["entry_price"]
        # 如果有未取消的掛單，先取消再下新單
        pending_oid = pos.get("pending_sell_oid")

    # 先取消任何殘留的未成交賣單（釋放被鎖定的代幣）
    if pending_oid:
        try:
            _api_call_with_timeout(client.cancel, pending_oid)
            print(f"🗑️ 已取消殘留掛單: {pending_oid[:16]}…")
        except Exception as ce:
            print(f"⚠️ 取消殘留掛單失敗: {ce}")
        with _positions_lock:
            if token_id in open_positions:
                open_positions[token_id].pop("pending_sell_oid", None)
        time.sleep(1)  # 等待取消生效

    try:
        book = _api_call_with_timeout(client.get_order_book, token_id)
        best_bid, best_ask = _parse_orderbook(book)
        if best_bid is None or best_ask is None:
            send(f"⚠️ 平倉失敗：訂單簿無流動性\n"
                 f"Token: {token_id[:12]}…\n"
                 f"持倉: {size:.2f} 份 @ {entry_price:.3f}")
            return

        # 5 分鐘二元市場策略：無條件果斷賣出，不設價格底線
        # 掛在 best_bid 下方確保立即成交
        limit_price = max(best_bid - 0.01, 0.01)
        limit_price = round(limit_price, 3)

        # 確保賣出量不超過實際持倉（避免 not enough balance）
        safe_size = round(min(size, size * 0.99), 2)
        if safe_size < 0.01:
            safe_size = size  # 數量太小時不再縮減

        print(f"📤 平倉下單: price={limit_price} size={safe_size} bid={best_bid} ask={best_ask}")

        order_args   = OrderArgs(price=limit_price, size=safe_size,
                                 side=SELL, token_id=token_id)
        signed_order = _api_call_with_timeout(client.create_order, order_args)
        resp         = _api_call_with_timeout(client.post_order, signed_order)

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
            return

        oid = _get_order_id(resp)
        exit_price = limit_price  # 估算值

        if oid:
            filled, exit_price, _ = _poll_order_matched(oid, limit_price)
            if not filled:
                send(f"⚠️ 平倉訂單超時未成交，嘗試取消\n"
                     f"訂單 ID: {oid[:16]}…\n"
                     f"嘗試賣出: {safe_size:.2f} 份 @ {limit_price:.3f}")
                try:
                    _api_call_with_timeout(client.cancel, oid)
                    print(f"🗑️ 已取消超時賣單: {oid[:16]}…")
                except Exception as ce:
                    # 取消失敗 → 記錄掛單 ID，下次進來先取消
                    print(f"⚠️ 取消賣單失敗: {ce}")
                    with _positions_lock:
                        if token_id in open_positions:
                            open_positions[token_id]["pending_sell_oid"] = oid
                return  # 放棄本次平倉，交給下一次輪詢處理
        else:
            send("⚠️ 無法取得平倉訂單 ID，以限價估算退出價格")

        slippage_pct = round((exit_price - best_bid) / best_bid, 6) if best_bid else 0
        
        # 估算 Polymarket 5m 短期市場 Taker Fee (雙向各約 1.56%)
        # 因為手續費會直接從錢包扣 USDC，我們在 PnL 反映真實淨值
        fee_rate = 0.0156
        entry_fee = entry_price * size * fee_rate
        exit_fee  = exit_price * size * fee_rate
        total_fee = entry_fee + exit_fee
        
        realized_pnl = (exit_price - entry_price) * size - total_fee

        log_trade({
            "date":         datetime.date.today().isoformat(),
            "timestamp":    datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "token_id":     token_id,
            "side":         "sell",
            "entry_price":  round(entry_price, 4),
            "exit_price":   round(exit_price, 4),
            "size":         round(size, 2),
            "slippage_pct": slippage_pct,
            "realized_pnl": round(realized_pnl, 4),
            "fees":         round(total_fee, 4),
            "status":       "closed",
            "hold_time":    (datetime.datetime.now(datetime.timezone.utc) - pos["opened_at"]).total_seconds()
        })
        hold_time = (datetime.datetime.now(datetime.timezone.utc)
                     - pos["opened_at"]).total_seconds()
        pnl_pct = (realized_pnl / (entry_price * size) * 100) if (entry_price * size) else 0
        pnl_emoji = "🟢" if realized_pnl >= 0 else "🔴"
        send(f"📤 平倉完成\n"
             f"{'─'*28}\n"
             f"📋 {pos.get('question', 'N/A')[:40]}\n"
             f"Token: {token_id[:12]}…\n"
             f"進場: {entry_price:.3f} → 出場: {exit_price:.3f}\n"
             f"數量: {size:.2f} 份 | 持倉: {int(hold_time)}s\n"
             f"滑點: {slippage_pct*100:.3f}%\n"
             f"{pnl_emoji} PnL: {realized_pnl:+.4f} USDC ({pnl_pct:+.2f}%)\n"
             f"{'─'*28}")

        with _positions_lock:
            open_positions.pop(token_id, None)

        # 熔斷邏輯
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
            # 用即時的 open_positions 檢查 phantom_warned，避免 copy() 快照問題
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


# ======================================================
# 📊  持倉監控（止盈 / 止損 / 超時）
# ======================================================

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
            entry_price  = pos["entry_price"]
            hold_seconds = (now_dt - pos["opened_at"]).total_seconds()
            
            # 絕對超時防呆清除：市場結束後再等 3 分鐘仍在追蹤則強制清除
            # pos["time_left"] 是建倉時的市場剩餘秒數
            # 市場結束後經過的秒數 = hold_seconds - time_left (確保非負)
            time_since_market_end = hold_seconds - pos["time_left"]
            if time_since_market_end > 180:  # 市場結算後超過 3 分鐘
                send(f"🧹 市場結算超時，自動清除本地追蹤\n"
                     f"📋 {pos.get('question', 'N/A')[:40]}\n"
                     f"持倉時間: {int(hold_seconds)}s | 市場已結算約: {int(time_since_market_end)}s 前\n"
                     f"（請至 Polymarket 查看結算結果）")
                with _positions_lock:
                    open_positions.pop(token_id, None)
                continue

            try:
                book = _api_call_with_timeout(client.get_order_book, token_id)
                best_bid, _ = _parse_orderbook(book)
            except Exception as e:
                print(f"📊 持倉監控 - 訂單簿查詢失敗: {e} | token={token_id[:12]}…")
                # 如果持倉超過 5 分鐘且訂單簿無法查詢，市場可能已結算
                if hold_seconds > 330:
                    from btc5m.utils import get_usdc_balance
                    settle_bal = get_usdc_balance()
                    bal_str = f"{settle_bal:.4f} USDC" if settle_bal >= 0 else "查詢失敗"
                    # 取得市場方算时間段標識
                    mkt_end_dt = pos.get('opened_at') + datetime.timedelta(seconds=pos.get('time_left', 0))
                    mkt_label  = mkt_end_dt.strftime('%H:%M') + ' UTC'
                    send(f"🏁 市場已結算（訂單簿查詢失敗）\n"
                         f"📋 {pos.get('question', 'N/A')[:40]}\n"
                         f"市場結束: {mkt_label}\n"
                         f"持倉時間: {int(hold_seconds)}s\n"
                         f"💰 結算後餘額: {bal_str}\n"
                         f"自動清除持倉記錄")
                    with _positions_lock:
                        open_positions.pop(token_id, None)
                continue

            if best_bid is None:
                # 訂單簿為空，可能市場已結算
                if hold_seconds > 330:
                    from btc5m.utils import get_usdc_balance
                    settle_bal = get_usdc_balance()
                    bal_str = f"{settle_bal:.4f} USDC" if settle_bal >= 0 else "查詢失敗"
                    mkt_end_dt = pos.get('opened_at') + datetime.timedelta(seconds=pos.get('time_left', 0))
                    mkt_label  = mkt_end_dt.strftime('%H:%M') + ' UTC'
                    send(f"🏁 市場已結算（訂單簿為空）\n"
                         f"📋 {pos.get('question', 'N/A')[:40]}\n"
                         f"市場結束: {mkt_label}\n"
                         f"進場: {entry_price:.3f} | 數量: {pos['size']:.2f}\n"
                         f"持倉時間: {int(hold_seconds)}s\n"
                         f"💰 結算後餘額: {bal_str}\n"
                         f"自動清除持倉記錄，請到 Polymarket 領取結算獎金")
                    with _positions_lock:
                        open_positions.pop(token_id, None)
                else:
                    print(f"⚠️ 訂單簿為空但未超時 ({int(hold_seconds)}s) | token={token_id[:12]}…")
                continue

            # 強制轉換為進場價的絕對百分比，且設置 0.99 上限（代幣最大價值為 1 鎂）
            tp_target = min(entry_price * (1 + pos["tp_pct"]), 0.99)
            if tp_target <= entry_price:
                tp_target = entry_price + 0.01 # 防呆機制，確保有獲利空間
                
            sl_target = entry_price * (1 - sl_pct)

            unrealized_pct = (best_bid - entry_price) / entry_price
            if unrealized_pct > 0.05:
                max_hold = min(POS_MAX_HOLD_SEC, max(pos["time_left"] - 60, 20))
            elif unrealized_pct < -0.03:
                max_hold = min(POS_MAX_HOLD_SEC, max(pos["time_left"] - 15, 45))
            else:
                max_hold = min(POS_MAX_HOLD_SEC, max(pos["time_left"] - 30, 30))

            reason = None
            if best_bid >= tp_target:
                reason = "🎯 達到動態止盈"
            elif best_bid <= sl_target:
                reason = "🚨 觸發動態止損"
            elif hold_seconds >= max_hold:
                reason = "⏰ 結算規避/超時"

            if reason:
                fee_rate = 0.0156
                est_fee = entry_price * pos["size"] * fee_rate + best_bid * pos["size"] * fee_rate
                net_unrealized_pnl = (best_bid - entry_price) * pos["size"] - est_fee
                upnl_pct = unrealized_pct * 100

                # 通知去重：同一原因只發一次 Telegram，避免結算延遲時刷屏
                # 若原因「改變」（如從止盈跌入止損區），則重新發送
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
                # 只有當 _close_position 成功移除持倉後才設定冷卻
                with _positions_lock:
                    if token_id not in open_positions:
                        _recently_closed[token_id] = time.time()
                _clean_recently_closed()
    finally:
        _manage_lock.release()


# ======================================================
# ⚡  主交易循環
# ======================================================

def analyze_and_trade():
    """
    主交易循環（每 20 秒觸發）：
      1. 管理現有持倉
      2. 檢查熔斷 / 每日風控
      3. 取得量化信號
      4. 查詢當前 BTC 5M 活躍市場
      5. 篩選合適標的並下單建倉
    """
    if not _analyze_lock.acquire(blocking=False):
        return
    try:
        manage_positions()

        # 熔斷冷卻檢查
        with _pause_until_lock:
            pause = cfg._pause_until
        if time.time() < pause:
            print(f"🛡️ 熔斷冷卻中，剩餘 {int(pause - time.time())}s")
            return

        # 每日風控
        pnl_today = get_daily_realized_pnl()
        if pnl_today < -START_CAPITAL * DAILY_MAX_LOSS:
            send(f"🚫 已觸及單日最大虧損，今日停止交易\n"
                 f"今日 PnL: {pnl_today:+.4f} USDC\n"
                 f"虧損上限: {-START_CAPITAL * DAILY_MAX_LOSS:.2f} USDC")
            return
        if pnl_today > START_CAPITAL * DAILY_TAKE_PROFIT:
            send(f"🏆 已達單日止盈目標，今日停止交易\n"
                 f"今日 PnL: {pnl_today:+.4f} USDC\n"
                 f"止盈目標: {START_CAPITAL * DAILY_TAKE_PROFIT:.2f} USDC")
            return

        # 持倉上限
        with _positions_lock:
            if len(open_positions) >= MAX_POSITIONS:
                return

        # 量化信號
        btc_info   = get_btc_signals()
        signal_dir = btc_info["signal"]
        if signal_dir == 0:
            return  # 診斷輸出已在 get_btc_signals() 內完成

        dir_str = "看漲 (買UP/YES)" if signal_dir == 1 else "看跌 (買DOWN/NO)"
        score = btc_info['bull_score'] if signal_dir == 1 else btc_info['bear_score']
        conf_tag = " 🔥高信心" if btc_info.get('high_conf') else ""
        
        # 不要立刻發送，先存成字串，確認有下單條件再發送
        signal_msg = (f"⚡ 捕捉到信號！{conf_tag}\n"
             f"{'─'*28}\n"
             f"方向: {dir_str}\n"
             f"積分: 🐂{btc_info['bull_score']} / 🐻{btc_info['bear_score']}\n"
             f"BTC: ${btc_info['close']:,.1f}\n"
             f"RSI: {btc_info['rsi']:.1f} | ADX: {btc_info['adx']:.1f} | ATR: {btc_info['atr']:.2f}\n"
             f"趨勢: {'多頭' if btc_info['trend_bullish'] else '空頭'} | "
             f"MACD: {'擴展✅' if btc_info['bull_exp'] or btc_info['bear_exp'] else '收斂❌'} | "
             f"放量: {'✅' if btc_info['vol_ok'] else '❌'}\n"
             f"{'─'*28}")

        # 紀錄信號觸發次數（無論是否有效下單都先紀錄為「已捕獲」）
        with cfg._stats_lock:
            if signal_dir == 1:
                cfg.stats_signals_up += 1
            else:
                cfg.stats_signals_down += 1


        # 透過 Series API 動態取得當前窗口子市場
        markets = fetch_active_btc5m_markets()
        if not markets:
            print("⚠️ 找不到 BTC 5M 子市場")
            return

        print(f"\n🔎 開始掃描 {len(markets)} 個子市場...")

        for gm in markets:
            q = gm.get("question", "N/A")
            print(f"\n  📌 市場: {q[:50]}")

            # 跳過暫停接單的市場
            if not gm.get("acceptingOrders", False):
                print(f"     ❌ 跳過：acceptingOrders=False")
                continue

            # 跳過 negRisk 市場
            if gm.get("negRisk", False):
                print(f"     ❌ 跳過：negRisk 市場")
                continue

            # 迴避剩餘時間 < 60 秒的市場
            time_left    = 300
            end_date_str = gm.get("endDate") or gm.get("endDateIso", "")
            if end_date_str:
                try:
                    end_dt    = datetime.datetime.fromisoformat(
                                    end_date_str.replace("Z", "+00:00"))
                    time_left = (end_dt - datetime.datetime.now(
                                    datetime.timezone.utc)).total_seconds()
                    print(f"     ⏱  剩餘時間: {int(time_left)}s")
                    if time_left < 60:
                        print(f"     ❌ 跳過：剩餘時間 < 60s")
                        continue
                except ValueError:
                    pass

            # 解析目標代幣 ID
            target_token_id, outcome_label = _resolve_token_id(gm, signal_dir)
            if not target_token_id:
                print(f"     ❌ 跳過：無法解析代幣 ID (outcomes={gm.get('outcomes')})")
                continue
            print(f"     🎯 目標 outcome: {outcome_label}  token: {target_token_id[:12]}…")

            # 持倉重複與冷卻期檢查
            with _positions_lock:
                if target_token_id in open_positions:
                    print(f"     ❌ 跳過：此代幣已持倉")
                    continue
            if (target_token_id in _recently_closed and
                    time.time() - _recently_closed[target_token_id] < COOLDOWN_SEC):
                print(f"     ❌ 跳過：冷卻中")
                continue

            try:
                book = _api_call_with_timeout(client.get_order_book, target_token_id)
                best_bid, best_ask = _parse_orderbook(book)
                print(f"     📖 訂單簿: bid={best_bid}  ask={best_ask}")

                if best_bid is None or best_ask is None:
                    print(f"     ❌ 跳過：訂單簿無流動性")
                    continue

                # ATM 過濾
                if not (0.30 <= best_ask <= 0.70):
                    print(f"     ❌ 跳過：ask={best_ask:.3f} 不在 ATM 範圍 [0.30, 0.70]")
                    continue

                spread = best_ask - best_bid
                print(f"     📐 價差: {spread:.4f}  (需在 [{MIN_SPREAD}, {MAX_SPREAD}])")
                if spread < MIN_SPREAD or spread > MAX_SPREAD:
                    print(f"     ❌ 跳過：價差不符條件")
                    continue

                # 確保下單量 ≥ orderMinSize
                min_size = float(gm.get("orderMinSize") or 1)
                conf_multiplier = 1.5 if btc_info.get("high_conf") else 1.0
                size = max(round(MAX_USD * conf_multiplier / best_ask, 2), min_size)

                # ATR/ADX 自適應 TP/SL (單位: 絕對價格漲跌幅百分比)
                # 為了覆蓋約 3.12% 的雙向 Taker Fee，TP 最少必須大於手續費率 4% 以確保獲利
                tp_pct = 0.08  # 預設 8% (+4.88% 淨利)
                sl_pct = 0.10  # 預設 10% (-13.12% 淨損)
                if btc_info["adx"] > 25:
                    tp_pct = 0.12 # 趨勢強大時擴大獲利空間
                if btc_info["close"] > 0 and btc_info["atr"] / btc_info["close"] > 0.0015:
                    sl_pct = 0.15 # 波動極大時延遲止損
                    tp_pct = max(tp_pct, 0.15)

                rr_ratio    = tp_pct / sl_pct
                limit_price = round(
                    min(best_bid + spread * 0.5 * (1 + SLIPPAGE), best_ask), 3)

                # 包含 1.56% Taker fee 的建倉成本預估
                fee_rate = 0.0156
                cost_usdc = size * limit_price * (1 + fee_rate)
                
                tp_price = min(limit_price * (1 + tp_pct), 0.99)
                sl_price = limit_price * (1 - sl_pct)
                
                if signal_msg:
                    send(signal_msg)
                    signal_msg = None  # 只發送一次，避免多個子市場重複發送信號訊息
                
                # 下單前查詢餘額
                pre_bal = get_usdc_balance()
                with cfg._balance_lock:
                    cfg._pre_order_balance = pre_bal
                pre_bal_str = f"{pre_bal:.4f} USDC" if pre_bal >= 0 else "查詢失敗"

                send(f"💡 鎖定標的\n"
                     f"{'\u2500'*28}\n"
                     f"📋 {q[:45]}\n"
                     f"方向: {dir_str} ({outcome_label})\n"
                     f"💰 下單前餘額: {pre_bal_str}\n"
                     f"Bid: {best_bid:.3f} | Ask: {best_ask:.3f} | 價差: {spread:.4f}\n"
                     f"限價: {limit_price:.3f} | 數量: {size:.2f} 份\n"
                     f"預估成本(含手續費): ~{cost_usdc:.2f} USDC\n"
                     f"TP: {tp_pct*100:.0f}% → ~{tp_price:.3f}\n"
                     f"SL: {sl_pct*100:.0f}% → ~{sl_price:.3f}\n"
                     f"R:R 比: {rr_ratio:.1f} | 剩餘: {int(time_left)}s\n"
                     f"{'\u2500'*28}")

                order_args   = OrderArgs(price=limit_price, size=size,
                                         side=BUY, token_id=target_token_id)
                signed_order = _api_call_with_timeout(client.create_order, order_args)
                resp         = _api_call_with_timeout(client.post_order, signed_order)

                success = (getattr(resp, "success", False)
                           or (isinstance(resp, dict) and resp.get("success")))
                if not success:
                    err = (getattr(resp, "errorMsg", "")
                           or (resp.get("errorMsg", "") if isinstance(resp, dict) else ""))
                    send(f"⚠️ 下單失敗: {err}")
                    continue

                oid = _get_order_id(resp)
                if not oid:
                    send("⚠️ 無法取得訂單 ID，跳過建倉")
                    continue

                send(f"📨 訂單已送出 ID: {oid[:16]}…，等待成交…")

                # 輪詢等待成交；超時則取消訂單
                filled, fill_price, size_matched = _poll_order_matched(
                    oid, limit_price)
                if not filled:
                    try:
                        _api_call_with_timeout(client.cancel, oid)
                        send(f"⏰ 訂單 {oid[:12]}… 超時未成交，已取消")
                    except Exception:
                        pass
                    continue

                # === 建倉成功：等待鏈上結算 ===
                send("⏳ 等待鏈上結算 (10s)...")
                time.sleep(10)  # 等待 Polygon 鏈上結算完成

                # 下單後 10s 查詢餘額，追蹤餘額變化
                post_bal = get_usdc_balance()
                with cfg._balance_lock:
                    cfg._post_order_balance = post_bal

                # 使用 size_matched（訂單自身的成交量），比 get_trades 更可靠
                real_size = size_matched if size_matched > 0.001 else size
                if size_matched < 0.001:
                    send(f"⚠️ size_matched={size_matched}，使用下單量 {size} 作為持倉量")

                with _positions_lock:
                    open_positions[target_token_id] = {
                        "entry_price":  fill_price,
                        "size":         real_size,
                        "question":     q,
                        "opened_at":    datetime.datetime.now(datetime.timezone.utc),
                        "entry_spread": spread,
                        "time_left":    time_left,
                        "tp_pct":       tp_pct,
                        "sl_pct":       sl_pct,
                    }
                cost_usdc = real_size * fill_price
                tp_target = min(fill_price * (1 + tp_pct), 0.99)
                sl_target_price = fill_price * (1 - sl_pct)

                # 餘額變化計算
                with cfg._balance_lock:
                    pre_b  = cfg._pre_order_balance
                    post_b = cfg._post_order_balance
                if pre_b >= 0 and post_b >= 0:
                    bal_delta_str = f"\n💰 餘額變化: {pre_b:.4f} → {post_b:.4f} USDC ({post_b - pre_b:+.4f})"
                elif post_b >= 0:
                    bal_delta_str = f"\n💰 下單後餘額: {post_b:.4f} USDC"
                else:
                    bal_delta_str = ""

                send(f"📥 建倉成功！\n"
                     f"{'─'*28}\n"
                     f"📋 {q[:45]}\n"
                     f"方向: {dir_str} ({outcome_label})\n"
                     f"成交: {real_size:.2f} 份 @ {fill_price:.3f}\n"
                     f"成本: ~{cost_usdc:.2f} USDC\n"
                     f"TP: {tp_target:.3f} ({tp_pct*100:.0f}%)\n"
                     f"SL: {sl_target_price:.3f} ({sl_pct*100:.0f}%){bal_delta_str}\n"
                     f"最大持倉: {int(min(POS_MAX_HOLD_SEC, max(time_left - 30, 30)))}s | "
                     f"窗口剩餘: {int(time_left)}s\n"
                     f"{'─'*28}")

                
                with cfg._stats_lock:
                    cfg.stats_orders_placed += 1
                    
                return

            except Exception as e:
                send(f"❌ 下單異常: {e}")

        print("🔎 本輪掃描完畢，無符合條件的標的")

    except Exception as e:
        print(f"❌ 分析循環錯誤: {e}")
    finally:
        _analyze_lock.release()
