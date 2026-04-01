"""
btc5m.market — Polymarket 市場查詢（Gamma API）與代幣 ID 解析
"""

import time
import json
import datetime

from btc5m.config import (
    BTC5M_SERIES_ID, BTC5M_SERIES_SLUG,
    _market_cache, _market_cache_ts, _MARKET_CACHE_TTL,
)
from btc5m.utils import send, http_get_json, extract_list_payload, extract_object_payload

# 用模組層級可變變數追蹤快取（不能直接覆蓋 config 裡的 import）
import btc5m.config as _cfg


# ======================================================
# 🔍  Polymarket 市場查詢
# ======================================================

def fetch_active_btc5m_markets() -> list[dict]:
    """
    透過固定 Series ID 動態取得當前活躍的 BTC 5M 子市場列表。

    策略（雙層）：
      主力 → GET /series/10684
              從 events[] 中找 endDate 最近的活躍事件
      備援 → 時間戳推算 slug，直接查 GET /events/slug/{slug}

    加入 55 秒快取，避免每 20 秒都重複請求 API。
    """
    now_ts = time.time()
    if _cfg._market_cache and now_ts - _cfg._market_cache_ts < _cfg._MARKET_CACHE_TTL:
        return _cfg._market_cache  # 快取命中，直接回傳

    # ── 主力：Series API ────────────────────────────────
    try:
        payload = http_get_json(
            f"https://gamma-api.polymarket.com/series/{BTC5M_SERIES_ID}",
            timeout=8,
            retries=2,
        )
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        series_obj = extract_object_payload(payload)
        events = extract_list_payload(
            series_obj.get("events", []) if isinstance(series_obj, dict) else []
        )

        active_candidates = []
        for e in events:
            if not isinstance(e, dict):
                continue
            if not (e.get("active") and not e.get("closed")):
                continue
            end_raw = e.get("endDate") or e.get("endDateIso", "")
            if not end_raw:
                continue
            try:
                end_dt = datetime.datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if end_dt > now_dt:
                active_candidates.append((end_dt, e))

        if active_candidates:
            # 取 endDate 最近的事件（即當前 5 分鐘窗口）
            _, current_event = min(active_candidates, key=lambda x: x[0])
            markets = extract_list_payload(current_event.get("markets", []))
            if markets:
                _cfg._market_cache = markets
                _cfg._market_cache_ts = now_ts
                print(f"✅ [Series API] 窗口: {current_event.get('slug')} "
                      f"| {len(markets)} 個子市場")
                return markets
    except Exception as e:
        print(f"⚠️ Series API 失敗: {e}")

    # ── 備援：時間戳推算 slug ───────────────────────────
    base_ts = (int(now_ts) // 300) * 300
    for delta in (0, -300, 300):
        slug = f"btc-updown-5m-{base_ts + delta}"
        try:
            payload = http_get_json(
                f"https://gamma-api.polymarket.com/events/slug/{slug}",
                timeout=8,
                retries=2,
            )
            event = extract_object_payload(payload)
            markets = extract_list_payload(event.get("markets", []) if isinstance(event, dict) else [])
            if event.get("active") and not event.get("closed") and markets:
                _cfg._market_cache = markets
                _cfg._market_cache_ts = now_ts
                print(f"✅ [slug 備援] {slug} | {len(markets)} 個子市場")
                return markets
        except Exception as e:
            print(f"⚠️ slug 備援查詢失敗 ({slug}): {e}")

    send("🚨 所有市場查詢方案均失敗，請確認 BTC5M_SERIES_ID 是否仍有效")
    return []


def _resolve_token_id(gm: dict, signal_dir: int) -> tuple[str | None, str]:
    """
    從市場的 outcomes 欄位正確解析目標代幣 ID，不假設索引順序。
    signal_dir=1 → 找 Up/Yes/Above；signal_dir=-1 → 找 Down/No/Below。
    回傳 (token_id, outcome_label)，解析失敗時回傳 (None, "")。
    """
    try:
        raw_outcomes = gm.get("outcomes", '["Yes","No"]')
        outcomes = (json.loads(raw_outcomes)
                    if isinstance(raw_outcomes, str) else raw_outcomes)

        raw_ids  = gm.get("clobTokenIds", "[]")
        clob_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids

        if not outcomes or not clob_ids or len(outcomes) != len(clob_ids):
            return None, ""

        keywords = ("up", "yes", "above") if signal_dir == 1 else ("down", "no", "below")

        for i, outcome in enumerate(outcomes):
            if any(k in outcome.lower() for k in keywords):
                return str(clob_ids[i]).strip(), outcome

        # 關鍵字匹配失敗時的後備：多/空分別用索引 0/1
        idx = 0 if signal_dir == 1 else 1
        return str(clob_ids[idx]).strip(), outcomes[idx]

    except Exception as e:
        print(f"⚠️ 代幣 ID 解析失敗: {e}")
        return None, ""
