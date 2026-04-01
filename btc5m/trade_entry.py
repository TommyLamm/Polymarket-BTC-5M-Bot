"""
btc5m.trade_entry — 主交易循環（信號判斷、市場篩選、建倉）
"""

import time
import datetime

import btc5m.config as cfg
from btc5m.config import (
    client,
    MAX_USD, SLIPPAGE, MIN_SPREAD, MAX_SPREAD,
    ENTRY_COST_TOLERANCE_USD,
    DAILY_MAX_LOSS, DAILY_TAKE_PROFIT,
    POS_MAX_HOLD_SEC, MAX_POSITIONS, COOLDOWN_SEC,
    open_positions, _recently_closed,
    _positions_lock, _analyze_lock, _pause_until_lock,
)
from btc5m.utils import (
    send, _api_call_with_timeout,
    get_daily_realized_pnl, _parse_orderbook,
    _get_order_id, get_usdc_balance,
)
from btc5m.market import fetch_active_btc5m_markets, _resolve_token_id
from btc5m.signals import get_btc_signals
from btc5m.position_manager import manage_positions
from btc5m.order_execution_utils import (
    _normalize_order_status,
    _poll_order_matched,
    _cancel_order_and_validate,
    _quantize_down,
    _quantize_up,
    _extract_orderbook_constraints,
)
from btc5m.observability import (
    log_event,
    record_api_error,
    record_missed_trade,
    record_order_attempt,
    record_order_result,
    summarize_missed_trades,
)
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY

ATM_MIN_ASK = 0.30
ATM_MAX_ASK = 0.70
ENTRY_MIN_TIME_LEFT_SEC = 60


def _extract_order_fill_snapshot(order_state, fallback_price: float) -> tuple[str, float, float]:
    """從 get_order 回應提取狀態、成交價與成交量。"""
    status_raw = (getattr(order_state, "status", "") if hasattr(order_state, "status")
                  else order_state.get("status", "") if isinstance(order_state, dict) else "")
    status = _normalize_order_status(status_raw)

    size_matched = 0.0
    if hasattr(order_state, "size_matched"):
        try:
            size_matched = float(order_state.size_matched or 0)
        except (TypeError, ValueError):
            size_matched = 0.0
    elif isinstance(order_state, dict):
        raw_size = (
            order_state.get("size_matched")
            or order_state.get("sizeMatched")
            or order_state.get("filled_size")
            or order_state.get("filledSize")
            or order_state.get("matched_size")
            or order_state.get("matchedSize")
            or 0
        )
        try:
            size_matched = float(raw_size or 0)
        except (TypeError, ValueError):
            size_matched = 0.0

    avg_price = fallback_price
    raw_price = None
    if hasattr(order_state, "price"):
        raw_price = order_state.price
    elif isinstance(order_state, dict):
        raw_price = order_state.get("price") or order_state.get("avg_price") or order_state.get("avgPrice")
    if raw_price is not None:
        try:
            avg_price = float(raw_price)
        except (TypeError, ValueError):
            pass

    return status, avg_price, size_matched


def _confirm_entry_fill_after_timeout(
    order_id: str,
    fallback_price: float,
) -> tuple[bool, float, float]:
    """
    超時後二次查單：
    - 若可確認有成交（status/size_matched），回傳 (True, fill_price, size_matched)
    - 否則回傳 (False, fallback_price, 0.0)
    """
    confirmed_fill = False
    fill_price = fallback_price
    matched_size = 0.0
    terminal_unfilled = {"CANCELED", "REJECTED", "EXPIRED", "FAILED", "UNMATCHED"}
    fill_status = {"MATCHED", "FILLED", "PARTIALLY_MATCHED", "PARTIALLY_FILLED"}

    canceled = _cancel_order_and_validate(order_id, "進場超時")
    if not canceled:
        print(f"⚠️ 進場超時取消未完全確認：{order_id[:16]}…，改以查單結果判斷")

    for _ in range(3):
        try:
            st = _api_call_with_timeout(client.get_order, order_id)
            status, price, size = _extract_order_fill_snapshot(st, fallback_price)
            fill_price = price if price > 0 else fill_price
            matched_size = max(matched_size, size)
            print(f"🔎 超時後查單: status={status} | size_matched={size}")
            if status in fill_status and (size > 0 or status in {"MATCHED", "FILLED"}):
                confirmed_fill = True
                break
            if status in terminal_unfilled:
                break
        except Exception as confirm_err:
            print(f"⚠️ 超時後查單失敗: {confirm_err}")
        time.sleep(1)

    if confirmed_fill and matched_size <= 0:
        matched_size = 0.0
    return confirmed_fill, fill_price, matched_size


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

        with _pause_until_lock:
            pause = cfg._pause_until
        if time.time() < pause:
            print(f"🛡️ 熔斷冷卻中，剩餘 {int(pause - time.time())}s")
            return

        capital_base = float(cfg.START_CAPITAL)
        pnl_today = get_daily_realized_pnl()
        if pnl_today < -capital_base * DAILY_MAX_LOSS:
            send(f"🚫 已觸及單日最大虧損，今日停止交易\n"
                 f"今日 PnL: {pnl_today:+.4f} USDC\n"
                 f"虧損上限: {-capital_base * DAILY_MAX_LOSS:.2f} USDC")
            return
        if pnl_today > capital_base * DAILY_TAKE_PROFIT:
            send(f"🏆 已達單日止盈目標，今日停止交易\n"
                 f"今日 PnL: {pnl_today:+.4f} USDC\n"
                 f"止盈目標: {capital_base * DAILY_TAKE_PROFIT:.2f} USDC")
            return

        with _positions_lock:
            if len(open_positions) >= MAX_POSITIONS:
                return

        btc_info = get_btc_signals()
        signal_dir = btc_info["signal"]
        if signal_dir == 0:
            return

        dir_str = "看漲 (買UP/YES)" if signal_dir == 1 else "看跌 (買DOWN/NO)"
        conf_tag = " 🔥高信心" if btc_info.get("high_conf") else ""

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

        with cfg._stats_lock:
            if signal_dir == 1:
                cfg.stats_signals_up += 1
            else:
                cfg.stats_signals_down += 1

        markets = fetch_active_btc5m_markets()
        if not markets:
            print("⚠️ 找不到 BTC 5M 子市場")
            record_missed_trade("market_fetch_empty", "找不到活躍市場")
            return

        print(f"\n🔎 開始掃描 {len(markets)} 個子市場...")

        for gm in markets:
            q = gm.get("question", "N/A")
            print(f"\n  📌 市場: {q[:50]}")

            if not gm.get("acceptingOrders", False):
                print("     ❌ 跳過：acceptingOrders=False")
                record_missed_trade(
                    "market_not_accepting_orders",
                    "acceptingOrders=False",
                    market=q,
                )
                continue
            if gm.get("negRisk", False):
                print("     ❌ 跳過：negRisk 市場")
                record_missed_trade("market_neg_risk", "negRisk=True", market=q)
                continue

            time_left = 300
            end_date_str = gm.get("endDate") or gm.get("endDateIso", "")
            if end_date_str:
                try:
                    end_dt = datetime.datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    time_left = (end_dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
                    print(f"     ⏱  剩餘時間: {int(time_left)}s")
                    if time_left < ENTRY_MIN_TIME_LEFT_SEC:
                        print(f"     ❌ 跳過：剩餘時間 < {ENTRY_MIN_TIME_LEFT_SEC}s")
                        record_missed_trade(
                            "time_left_lt_60",
                            f"time_left={int(time_left)}",
                            market=q,
                        )
                        continue
                except ValueError:
                    pass

            target_token_id, outcome_label = _resolve_token_id(gm, signal_dir)
            if not target_token_id:
                print(f"     ❌ 跳過：無法解析代幣 ID (outcomes={gm.get('outcomes')})")
                record_missed_trade("token_unresolved", "resolve_token_id_failed", market=q)
                continue
            print(f"     🎯 目標 outcome: {outcome_label}  token: {target_token_id[:12]}…")

            with _positions_lock:
                if target_token_id in open_positions:
                    print("     ❌ 跳過：此代幣已持倉")
                    record_missed_trade(
                        "already_holding_token",
                        "token already in open_positions",
                        market=q,
                        token_id=target_token_id,
                    )
                    continue
            if (target_token_id in _recently_closed and
                    time.time() - _recently_closed[target_token_id] < COOLDOWN_SEC):
                print("     ❌ 跳過：冷卻中")
                record_missed_trade(
                    "token_cooldown",
                    "token within cooldown window",
                    market=q,
                    token_id=target_token_id,
                )
                continue

            try:
                book = _api_call_with_timeout(client.get_order_book, target_token_id)
                best_bid, best_ask = _parse_orderbook(book)
                print(f"     📖 訂單簿: bid={best_bid}  ask={best_ask}")

                if best_bid is None or best_ask is None:
                    print("     ❌ 跳過：訂單簿無流動性")
                    record_missed_trade(
                        "orderbook_no_liquidity",
                        "best_bid or best_ask is None",
                        market=q,
                        token_id=target_token_id,
                    )
                    continue

                if not (ATM_MIN_ASK <= best_ask <= ATM_MAX_ASK):
                    print(
                        f"     ❌ 跳過：ask={best_ask:.3f} 不在 ATM 範圍 "
                        f"[{ATM_MIN_ASK:.2f}, {ATM_MAX_ASK:.2f}]"
                    )
                    record_missed_trade(
                        "ask_not_atm",
                        f"ask={best_ask:.3f}",
                        market=q,
                        token_id=target_token_id,
                    )
                    continue

                spread = best_ask - best_bid
                print(f"     📐 價差: {spread:.4f}  (需在 [{MIN_SPREAD}, {MAX_SPREAD}])")
                if spread < MIN_SPREAD or spread > MAX_SPREAD:
                    print("     ❌ 跳過：價差不符條件")
                    record_missed_trade(
                        "spread_out_of_range",
                        f"spread={spread:.4f}",
                        market=q,
                        token_id=target_token_id,
                    )
                    continue

                tp_pct = 0.08
                sl_pct = 0.10
                if btc_info["adx"] > 25:
                    tp_pct = 0.12
                if btc_info["close"] > 0 and btc_info["atr"] / btc_info["close"] > 0.0015:
                    sl_pct = 0.15
                    tp_pct = max(tp_pct, 0.15)

                rr_ratio = tp_pct / sl_pct
                limit_price = round(min(best_bid + spread * 0.5 * (1 + SLIPPAGE), best_ask), 3)

                fee_rate = 0.0156
                cap_usd = float(MAX_USD)
                _, min_orderbook_size = _extract_orderbook_constraints(book)
                market_min_size = float(gm.get("orderMinSize") or 0.0)
                min_size = max(min_orderbook_size, market_min_size, 0.01)
                conf_multiplier = 1.5 if btc_info.get("high_conf") else 1.0
                proposed_size = max(round(cap_usd * conf_multiplier / best_ask, 2), 0.01)

                unit_cost = limit_price * (1 + fee_rate)
                if unit_cost <= 0:
                    print("     ❌ 跳過：單位成本無效")
                    record_missed_trade(
                        "unit_cost_invalid",
                        f"unit_cost={unit_cost}",
                        market=q,
                        token_id=target_token_id,
                    )
                    continue

                max_affordable_size = _quantize_down(cap_usd / unit_cost, 0.01, 0.0)
                if max_affordable_size <= 0:
                    print(f"     ❌ 跳過：1 USD 上限下不可下單 (unit_cost={unit_cost:.4f})")
                    record_missed_trade(
                        "max_affordable_size_zero",
                        f"cap={cap_usd},unit_cost={unit_cost:.4f}",
                        market=q,
                        token_id=target_token_id,
                    )
                    continue

                min_size_aligned = _quantize_up(min_size, 0.01, 0.01)
                if min_size_aligned - max_affordable_size > 1e-9:
                    print(
                        f"     ❌ 跳過：最小下單量超過風險上限 "
                        f"(min_size={min_size_aligned:.4f} > cap_size={max_affordable_size:.4f})"
                    )
                    record_missed_trade(
                        "min_size_over_cap",
                        f"min_size={min_size_aligned:.4f},cap_size={max_affordable_size:.4f}",
                        market=q,
                        token_id=target_token_id,
                    )
                    continue

                size = min(proposed_size, max_affordable_size)
                size = _quantize_down(size, 0.01, min_size_aligned)
                if size + 1e-9 < min_size_aligned:
                    print(
                        f"     ❌ 跳過：量化後數量低於最小下單量 "
                        f"(size={size:.4f}, min_size={min_size_aligned:.4f})"
                    )
                    record_missed_trade(
                        "quantized_size_below_min",
                        f"size={size:.4f},min_size={min_size_aligned:.4f}",
                        market=q,
                        token_id=target_token_id,
                    )
                    continue

                cost_usdc = size * limit_price * (1 + fee_rate)
                if cost_usdc > cap_usd + 1e-6:
                    print(
                        f"     ❌ 跳過：成本超過硬上限 "
                        f"(cost={cost_usdc:.4f} > cap={cap_usd:.4f})"
                    )
                    record_missed_trade(
                        "cost_over_cap",
                        f"cost={cost_usdc:.4f},cap={cap_usd:.4f}",
                        market=q,
                        token_id=target_token_id,
                    )
                    continue

                tp_price = min(limit_price * (1 + tp_pct), 0.99)
                sl_price = limit_price * (1 - sl_pct)

                if signal_msg:
                    send(signal_msg)
                    signal_msg = None

                pre_bal = get_usdc_balance()
                with cfg._balance_lock:
                    cfg._pre_order_balance = pre_bal
                pre_bal_str = f"{pre_bal:.4f} USDC" if pre_bal >= 0 else "查詢失敗"

                send(f"💡 鎖定標的\n"
                     f"{'─'*28}\n"
                     f"📋 {q[:45]}\n"
                     f"方向: {dir_str} ({outcome_label})\n"
                     f"💰 下單前餘額: {pre_bal_str}\n"
                     f"Bid: {best_bid:.3f} | Ask: {best_ask:.3f} | 價差: {spread:.4f}\n"
                     f"限價: {limit_price:.3f} | 數量: {size:.2f} 份\n"
                     f"預估成本(含手續費): ~{cost_usdc:.2f} USDC\n"
                     f"TP: {tp_pct*100:.0f}% → ~{tp_price:.3f}\n"
                     f"SL: {sl_pct*100:.0f}% → ~{sl_price:.3f}\n"
                     f"R:R 比: {rr_ratio:.1f} | 剩餘: {int(time_left)}s\n"
                     f"{'─'*28}")

                order_args = OrderArgs(price=limit_price, size=size, side=BUY, token_id=target_token_id)
                attempt_id = record_order_attempt(
                    scope="entry",
                    side="BUY",
                    token_id=target_token_id,
                    market=q,
                    limit_price=limit_price,
                    size=size,
                )
                signed_order = _api_call_with_timeout(client.create_order, order_args)
                resp = _api_call_with_timeout(client.post_order, signed_order)

                success = (getattr(resp, "success", False)
                           or (isinstance(resp, dict) and resp.get("success")))
                if not success:
                    err = (getattr(resp, "errorMsg", "")
                           or (resp.get("errorMsg", "") if isinstance(resp, dict) else ""))
                    record_order_result(
                        attempt_id=attempt_id,
                        scope="entry",
                        success=False,
                        error_text=err,
                        token_id=target_token_id,
                        market=q,
                    )
                    record_missed_trade(
                        "post_order_rejected",
                        err or "post_order returned success=False",
                        market=q,
                        token_id=target_token_id,
                    )
                    send(f"⚠️ 下單失敗: {err}")
                    continue

                oid = _get_order_id(resp)
                if not oid:
                    record_order_result(
                        attempt_id=attempt_id,
                        scope="entry",
                        success=False,
                        error_text="missing_order_id",
                        token_id=target_token_id,
                        market=q,
                    )
                    record_missed_trade(
                        "missing_order_id",
                        "post_order response missing order id",
                        market=q,
                        token_id=target_token_id,
                    )
                    send("⚠️ 無法取得訂單 ID，跳過建倉")
                    continue

                record_order_result(
                    attempt_id=attempt_id,
                    scope="entry",
                    success=True,
                    order_id=oid,
                    token_id=target_token_id,
                    market=q,
                )
                send(f"📨 訂單已送出 ID: {oid[:16]}…，等待成交…")

                filled, fill_price, size_matched = _poll_order_matched(oid, limit_price)
                if not filled:
                    confirmed_fill, fill_price, timeout_size_matched = _confirm_entry_fill_after_timeout(
                        oid, limit_price
                    )
                    if not confirmed_fill:
                        send(f"⏰ 訂單 {oid[:12]}… 超時未成交，已取消")
                        record_missed_trade(
                            "order_timeout_unfilled",
                            f"order {oid[:16]} timeout unfilled",
                            market=q,
                            token_id=target_token_id,
                            order_id=oid,
                        )
                        continue

                    size_matched = timeout_size_matched
                    send(
                        f"⚠️ 訂單 {oid[:12]}… 輪詢超時，但查單確認已有成交，"
                        "改為按已成交建倉"
                    )

                send("⏳ 等待鏈上結算 (10s)...")
                time.sleep(10)

                post_bal = get_usdc_balance()
                with cfg._balance_lock:
                    cfg._post_order_balance = post_bal

                real_size = size_matched if size_matched > 0.001 else size
                if size_matched < 0.001:
                    send(f"⚠️ size_matched={size_matched}，使用下單量 {size} 作為持倉量")

                actual_cost_usdc = real_size * fill_price * (1 + fee_rate)
                if actual_cost_usdc > cap_usd + float(ENTRY_COST_TOLERANCE_USD):
                    record_missed_trade(
                        "entry_actual_cost_exceeds_cap",
                        f"actual_cost={actual_cost_usdc:.4f},cap={cap_usd:.4f}",
                        market=q,
                        token_id=target_token_id,
                        order_id=oid,
                    )
                    send(
                        f"🚫 風控拒絕建倉：實際成本超過上限\n"
                        f"actual={actual_cost_usdc:.4f} > cap={cap_usd:.4f}\n"
                        f"訂單: {oid[:16]}…，嘗試立即取消/回退"
                    )
                    try:
                        _api_call_with_timeout(client.cancel, oid)
                    except Exception as cap_cancel_err:
                        print(f"⚠️ 超額建倉回退失敗: {cap_cancel_err}")
                    continue

                with _positions_lock:
                    open_positions[target_token_id] = {
                        "entry_price": fill_price,
                        "size": real_size,
                        "entry_cost_usdc": actual_cost_usdc,
                        "question": q,
                        "opened_at": datetime.datetime.now(datetime.timezone.utc),
                        "entry_spread": spread,
                        "time_left": time_left,
                        "tp_pct": tp_pct,
                        "sl_pct": sl_pct,
                    }
                cost_usdc = actual_cost_usdc
                tp_target = min(fill_price * (1 + tp_pct), 0.99)
                sl_target_price = fill_price * (1 - sl_pct)

                with cfg._balance_lock:
                    pre_b = cfg._pre_order_balance
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
                record_api_error(
                    "trade_entry_market_loop",
                    e,
                    market=q,
                    token_id=target_token_id if "target_token_id" in locals() else "",
                )
                if "get_order_book" in str(e):
                    record_missed_trade(
                        "orderbook_fetch_error",
                        str(e),
                        market=q,
                        token_id=target_token_id if "target_token_id" in locals() else "",
                    )
                send(f"❌ 下單異常: {e}")

        missed_summary = summarize_missed_trades(window_sec=3600)
        if missed_summary["total"] > 0:
            msg = (
                f"📉 Missed Trades 摘要（近1小時）\n"
                f"總數: {missed_summary['total']}\n"
                f"{missed_summary['headline']}\n"
                f"建議: {missed_summary['recommendation']}"
            )
            print(msg)
            log_event("missed_trade_summary", **missed_summary)
        print("🔎 本輪掃描完畢，無符合條件的標的")

    except Exception as e:
        print(f"❌ 分析循環錯誤: {e}")
    finally:
        _analyze_lock.release()
