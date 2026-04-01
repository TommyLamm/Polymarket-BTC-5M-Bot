"""
observe_three_orders.py — 實際執行交易循環並觀測 3 次送單嘗試

行為：
1) 啟動觀測事件輸出（JSONL）
2) 迴圈執行 analyze_and_trade / manage_positions
3) 以「order_attempt」累計達 3 次即結束（成功/失敗皆算）
4) 最多執行 60 分鐘，超時則輸出目前統計
5) 使用 Polymarket API 補抓 order_result 中的訂單狀態
"""

from __future__ import annotations

import argparse
import json
import os
import time
import datetime
from pathlib import Path

from btc5m.config import client
import btc5m.trade_entry as trade_entry_mod
from btc5m.position_manager import manage_positions
from btc5m.utils import _api_call_with_timeout
from btc5m.observability import (
    configure_observability,
    reset_events,
    get_events,
    count_order_attempts,
    summarize_missed_trades,
    summarize_api_health,
    log_event,
)


def _session_output_dir() -> Path:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path("observation_logs") / f"run_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _enrich_orders() -> list[dict]:
    results = get_events(stage="order_result")
    enriched = []
    for row in results:
        order_id = str(row.get("order_id") or "").strip()
        if not order_id:
            continue
        item = dict(row)
        try:
            st = _api_call_with_timeout(client.get_order, order_id, timeout=10)
            if hasattr(st, "dict"):
                item["order_snapshot"] = st.dict()
            elif hasattr(st, "__dict__"):
                item["order_snapshot"] = st.__dict__
            else:
                item["order_snapshot"] = st
        except Exception as e:
            item["order_snapshot_error"] = str(e)
        enriched.append(item)
    return enriched


def _forced_signal_payload(signal_dir: int) -> dict:
    bull = 75 if signal_dir == 1 else 25
    bear = 75 if signal_dir == -1 else 25
    return {
        "signal": signal_dir,
        "high_conf": True,
        "bull_score": bull,
        "bear_score": bear,
        "adx": 30.0,
        "atr": 80.0,
        "close": 0.0,
        "rsi": 50.0,
        "trend_bullish": signal_dir == 1,
        "bull_exp": signal_dir == 1,
        "bear_exp": signal_dir == -1,
        "vol_ok": True,
    }


def _maybe_force_signal(force_signal: str):
    if force_signal not in {"up", "down"}:
        return
    signal_dir = 1 if force_signal == "up" else -1

    def _forced():
        return _forced_signal_payload(signal_dir)

    trade_entry_mod.get_btc_signals = _forced
    print(f"⚠️ 已啟用強制信號模式: {force_signal.upper()}（僅本觀測腳本）")


def _build_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Observe 3 real order attempts with telemetry.")
    p.add_argument("--target-attempts", type=int, default=3, help="Target entry order attempts.")
    p.add_argument("--max-runtime-min", type=int, default=60, help="Max runtime in minutes.")
    p.add_argument("--sleep-sec", type=float, default=2, help="Loop sleep seconds.")
    p.add_argument("--override-max-usd", type=float, default=None, help="Override entry MAX_USD.")
    p.add_argument("--override-slippage", type=float, default=None, help="Override entry SLIPPAGE.")
    p.add_argument("--override-cooldown-sec", type=int, default=None, help="Override COOLDOWN_SEC.")
    p.add_argument("--override-min-time-left-sec", type=int, default=None, help="Override min time-left gate.")
    p.add_argument("--override-atm-min-ask", type=float, default=None, help="Override ATM min ask.")
    p.add_argument("--override-atm-max-ask", type=float, default=None, help="Override ATM max ask.")
    p.add_argument(
        "--relax-spread",
        action="store_true",
        help="Temporarily set MIN_SPREAD=0.005 for observation run.",
    )
    p.add_argument(
        "--force-signal",
        choices=["none", "up", "down"],
        default=os.getenv("OBS_FORCE_SIGNAL", "none"),
        help="Force trading signal direction for observation only.",
    )
    return p.parse_args()


def main():
    args = _build_args()
    _maybe_force_signal(args.force_signal)
    if args.relax_spread:
        trade_entry_mod.MIN_SPREAD = 0.005
        print("⚠️ 已啟用觀測專用價差放寬：MIN_SPREAD=0.005（僅本觀測腳本）")
    if args.override_max_usd is not None:
        trade_entry_mod.MAX_USD = float(args.override_max_usd)
        print(f"⚠️ 已覆寫 MAX_USD={trade_entry_mod.MAX_USD}（僅本觀測腳本）")
    if args.override_slippage is not None:
        trade_entry_mod.SLIPPAGE = float(args.override_slippage)
        print(f"⚠️ 已覆寫 SLIPPAGE={trade_entry_mod.SLIPPAGE}（僅本觀測腳本）")
    if args.override_cooldown_sec is not None:
        trade_entry_mod.COOLDOWN_SEC = int(args.override_cooldown_sec)
        print(f"⚠️ 已覆寫 COOLDOWN_SEC={trade_entry_mod.COOLDOWN_SEC}（僅本觀測腳本）")
    if args.override_min_time_left_sec is not None:
        trade_entry_mod.ENTRY_MIN_TIME_LEFT_SEC = int(args.override_min_time_left_sec)
        print(
            f"⚠️ 已覆寫 ENTRY_MIN_TIME_LEFT_SEC={trade_entry_mod.ENTRY_MIN_TIME_LEFT_SEC} "
            f"（僅本觀測腳本）"
        )
    if args.override_atm_min_ask is not None:
        trade_entry_mod.ATM_MIN_ASK = float(args.override_atm_min_ask)
        print(f"⚠️ 已覆寫 ATM_MIN_ASK={trade_entry_mod.ATM_MIN_ASK}（僅本觀測腳本）")
    if args.override_atm_max_ask is not None:
        trade_entry_mod.ATM_MAX_ASK = float(args.override_atm_max_ask)
        print(f"⚠️ 已覆寫 ATM_MAX_ASK={trade_entry_mod.ATM_MAX_ASK}（僅本觀測腳本）")

    target_attempts = max(1, int(args.target_attempts))
    max_runtime_sec = max(60, int(args.max_runtime_min) * 60)
    loop_sleep_sec = max(0.5, float(args.sleep_sec))

    out_dir = _session_output_dir()
    events_path = out_dir / "events.jsonl"
    report_path = out_dir / "summary.json"

    configure_observability(
        enabled=True,
        events_path=str(events_path),
        session_label=out_dir.name,
    )
    reset_events()
    print(f"📁 觀測輸出目錄: {out_dir}")
    print(f"🎯 目標送單次數: {target_attempts}（包含成功/失敗）")
    print(f"⏳ 最長執行時間: {max_runtime_sec // 60} 分鐘")
    start_ts = time.time()

    try:
        while True:
            elapsed = time.time() - start_ts
            if elapsed > max_runtime_sec:
                print("⏰ 已達最大觀測時長，停止執行。")
                log_event("observer_timeout", elapsed_sec=elapsed, target_attempts=target_attempts)
                break

            attempts = count_order_attempts(scope="entry")
            if attempts >= target_attempts:
                print(f"✅ 已達 {attempts} 次送單嘗試，停止執行。")
                break

            trade_entry_mod.analyze_and_trade()
            manage_positions()
            time.sleep(loop_sleep_sec)

    except KeyboardInterrupt:
        print("🛑 手動中止觀測。")
        log_event("observer_interrupted")

    attempts = count_order_attempts(scope="entry")
    order_results = get_events(stage="order_result")
    missed_summary = summarize_missed_trades(window_sec=3600)
    api_health = summarize_api_health(window_sec=3600)
    enriched_orders = _enrich_orders()

    final_report = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "target_attempts": target_attempts,
        "actual_attempts": attempts,
        "max_runtime_sec": max_runtime_sec,
        "force_signal_mode": args.force_signal,
        "missed_trade_summary_1h": missed_summary,
        "api_health_1h": api_health,
        "order_results": order_results,
        "enriched_orders_from_polymarket_api": enriched_orders,
        "events_file": str(events_path),
    }

    report_path.write_text(json.dumps(final_report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n================ 觀測摘要 ================")
    print(f"送單嘗試: {attempts}/{target_attempts}")
    print(f"Missed Trades: {missed_summary['total']}")
    print(f"Top Missed 原因: {missed_summary['top_reason_label'] or 'N/A'}")
    print(f"API 錯誤: {api_health['api_error_total']}")
    print(f"RPC 警告: {api_health['rpc_warning_total']}")
    print(f"報告檔案: {report_path}")
    print("=========================================\n")


if __name__ == "__main__":
    main()
