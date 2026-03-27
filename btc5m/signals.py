"""
btc5m.signals — 幣安現貨量化指標引擎（加權積分信號系統）
"""

import datetime

import pandas as pd
import numpy as np
import requests


# ======================================================
# 📈  多時框加權積分信號引擎
# ======================================================

def get_btc_signals() -> dict:
    """
    加權積分信號系統（取代原有四條件 AND 邏輯）
    多頭/空頭各有最多 100 分，達到 55 分即觸發，以量取勝。
    """
    try:
        # ── 4H 大趨勢（保留原邏輯）──────────────────────────
        r4h = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "4h", "limit": 60},
            timeout=5
        ).json()
        df4 = pd.DataFrame(r4h, columns=['t','o','h','l','c','v','T','qav','nt','tbv','tqv','i'])
        df4['c'] = df4['c'].astype(float)
        ema_4h        = df4['c'].ewm(span=50, adjust=False).mean().iloc[-1]
        trend_bullish = df4['c'].iloc[-1] > ema_4h

        # ── 5m K線資料（保留原邏輯）─────────────────────────
        r5m = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "5m", "limit": 100},
            timeout=5
        ).json()
        df = pd.DataFrame(r5m, columns=['t','o','h','l','c','v','T','qav','nt','tbv','tqv','i'])
        for col in ['o','h','l','c','v']:
            df[col] = df[col].astype(float)

        # ── RSI(14) ──────────────────────────────────────────
        delta = df['c'].diff()
        gain  = delta.where(delta > 0, 0).ewm(alpha=1/14, adjust=False).mean()
        loss  = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        df['rsi'] = 100 - (100 / (1 + gain / loss))

        # ── MACD(12,26,9) ────────────────────────────────────
        df['macd']     = (df['c'].ewm(span=12, adjust=False).mean()
                        - df['c'].ewm(span=26, adjust=False).mean())
        df['sig_line'] = df['macd'].ewm(span=9, adjust=False).mean()
        df['hist']     = df['macd'] - df['sig_line']

        # ── ATR(14) ──────────────────────────────────────────
        tr = pd.concat([
            df['h'] - df['l'],
            (df['h'] - df['c'].shift()).abs(),
            (df['l'] - df['c'].shift()).abs(),
        ], axis=1).max(axis=1)
        df['atr'] = tr.rolling(14).mean()

        # ── ADX(14) ──────────────────────────────────────────
        up       = df['h'].diff()
        dn       = df['l'].shift(1) - df['l']
        plus_dm  = np.where((up > dn) & (up > 0), up, 0.0)
        minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
        p_di = (100 * pd.Series(plus_dm).ewm(alpha=1/14, adjust=False).mean() / df['atr'])
        m_di = (100 * pd.Series(minus_dm).ewm(alpha=1/14, adjust=False).mean() / df['atr'])
        dx   = 100 * (p_di - m_di).abs() / (p_di + m_di).replace(0, 1)
        df['adx'] = dx.ewm(alpha=1/14, adjust=False).mean()

        # ── 1m 超短線動能（新增）────────────────────────────
        r1m = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1m", "limit": 30},
            timeout=5
        ).json()
        df1 = pd.DataFrame(r1m, columns=['t','o','h','l','c','v','T','qav','nt','tbv','tqv','i'])
        df1['c'] = df1['c'].astype(float)
        df1['v'] = df1['v'].astype(float)
        # 1m 快速 EMA 多空排列
        ema3_1m  = df1['c'].ewm(span=3,  adjust=False).mean()
        ema8_1m  = df1['c'].ewm(span=8,  adjust=False).mean()
        ema21_1m = df1['c'].ewm(span=21, adjust=False).mean()
        micro_bull = (ema3_1m.iloc[-1] > ema8_1m.iloc[-1] > ema21_1m.iloc[-1])
        micro_bear = (ema3_1m.iloc[-1] < ema8_1m.iloc[-1] < ema21_1m.iloc[-1])
        # 1m 成交量動能
        vol1m_ratio = df1['v'].iloc[-1] / df1['v'].rolling(10).mean().iloc[-1]

        # ── 15m 趨勢確認（新增）─────────────────────────────
        r15m = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "15m", "limit": 30},
            timeout=5
        ).json()
        df15 = pd.DataFrame(r15m, columns=['t','o','h','l','c','v','T','qav','nt','tbv','tqv','i'])
        df15['c'] = df15['c'].astype(float)
        ema9_15m  = df15['c'].ewm(span=9,  adjust=False).mean()
        ema20_15m = df15['c'].ewm(span=20, adjust=False).mean()
        mid_bull  = df15['c'].iloc[-1] > ema9_15m.iloc[-1] > ema20_15m.iloc[-1]
        mid_bear  = df15['c'].iloc[-1] < ema9_15m.iloc[-1] < ema20_15m.iloc[-1]

        df['vol_sma'] = df['v'].rolling(20).mean()
        curr, prev, prev2 = df.iloc[-1], df.iloc[-2], df.iloc[-3]

        # ── 原有條件 ──────────────────────────────────────────
        bull_exp = (curr['hist'] > 0 and curr['hist'] > prev['hist'] > prev2['hist'])
        bear_exp = (curr['hist'] < 0 and curr['hist'] < prev['hist'] < prev2['hist'])
        vol_ok   = curr['v'] > 1.5 * curr['vol_sma']

        # ── 新增條件：Stochastic RSI ─────────────────────────
        rsi_series  = df['rsi']
        rsi_min     = rsi_series.rolling(14).min()
        rsi_max     = rsi_series.rolling(14).max()
        stoch_rsi   = (rsi_series - rsi_min) / (rsi_max - rsi_min + 1e-9)
        stoch_k     = stoch_rsi.rolling(3).mean()
        stoch_d     = stoch_k.rolling(3).mean()
        stoch_bull  = (stoch_k.iloc[-1] > stoch_d.iloc[-1] and stoch_k.iloc[-1] < 0.8)
        stoch_bear  = (stoch_k.iloc[-1] < stoch_d.iloc[-1] and stoch_k.iloc[-1] > 0.2)

        # ── 新增條件：價格動能（連續 K 線方向）────────────────
        last3_bull  = all(df['c'].iloc[-i] > df['o'].iloc[-i] for i in range(1, 4))
        last3_bear  = all(df['c'].iloc[-i] < df['o'].iloc[-i] for i in range(1, 4))

        # ╔══════════════════════════════════════════════════╗
        # ║            加權積分計算（核心改動）               ║
        # ╚══════════════════════════════════════════════════╝
        #
        # 分組設計：
        #   A. 趨勢對齊（最高權重）
        #   B. 動能指標（中等權重）
        #   C. 短線確認（較低權重）
        # 達到 55 分觸發；高信心閾值 70 分可加倉

        bull_score = 0
        bear_score = 0

        # A. 趨勢對齊（合計 45 分）
        if trend_bullish:       bull_score += 20  # 4H EMA 大趨勢
        if not trend_bullish:   bear_score += 20
        if mid_bull:            bull_score += 15  # 15m 中期趨勢
        if mid_bear:            bear_score += 15
        if micro_bull:          bull_score += 10  # 1m 微觀動能
        if micro_bear:          bear_score += 10

        # B. 動能指標（合計 35 分）
        if bull_exp:            bull_score += 15  # MACD 擴展
        if bear_exp:            bear_score += 15
        if stoch_bull:          bull_score += 10  # Stoch RSI 金叉
        if stoch_bear:          bear_score += 10
        if curr['rsi'] < 65:    bull_score += 5   # RSI 未超買
        if curr['rsi'] > 35:    bear_score += 5   # RSI 未超賣

        # C. 短線確認（合計 20 分）
        if last3_bull:          bull_score += 10  # 3 根陽線
        if last3_bear:          bear_score += 10
        if vol_ok:              bull_score += 5   # 放量
        if vol_ok:              bear_score += 5   # 放量（雙向有效）
        if vol1m_ratio > 1.3:   bull_score += 5   # 1m 成交量放大
        if vol1m_ratio > 1.3:   bear_score += 5

        # ── 觸發閾值 ─────────────────────────────────────────
        SIGNAL_THRESHOLD      = 55  # 常規觸發
        HIGH_CONF_THRESHOLD   = 70  # 高信心（可配合加倉邏輯）

        signal    = 0
        high_conf = False

        if bull_score >= SIGNAL_THRESHOLD and bull_score > bear_score:
            signal    = 1
            high_conf = bull_score >= HIGH_CONF_THRESHOLD
        elif bear_score >= SIGNAL_THRESHOLD and bear_score > bull_score:
            signal    = -1
            high_conf = bear_score >= HIGH_CONF_THRESHOLD

        # ── 診斷輸出 ─────────────────────────────────────────
        signal_str = {1: "✅ 做多", -1: "✅ 做空", 0: "⏳ 觀望"}.get(signal, "?")
        trend_str  = (f"多頭 (收{curr['c']:.1f} > EMA {ema_4h:.1f})"
                      if trend_bullish else f"空頭 (收{curr['c']:.1f} < EMA {ema_4h:.1f})")
        conf_tag   = " 🔥高信心" if high_conf else ""
        print(
            f"\n{'─'*55}\n"
            f"📊 積分信號 [{datetime.datetime.now().strftime('%H:%M:%S')}]\n"
            f"  4H 趨勢  : {trend_str}\n"
            f"  15m 趨勢 : {'多頭' if mid_bull else '空頭' if mid_bear else '中性'}\n"
            f"  1m  動能 : {'多頭' if micro_bull else '空頭' if micro_bear else '中性'}  "
            f"Vol×{vol1m_ratio:.2f}\n"
            f"  5m RSI   : {curr['rsi']:.1f}  StochRSI K:{stoch_k.iloc[-1]:.2f} D:{stoch_d.iloc[-1]:.2f}\n"
            f"  MACD hist: {curr['hist']:.4f}/{prev['hist']:.4f}/{prev2['hist']:.4f}\n"
            f"  ADX      : {curr['adx']:.1f}  ATR: {curr['atr']:.2f}\n"
            f"  🐂 多頭積分: {bull_score}/100  🐻 空頭積分: {bear_score}/100\n"
            f"  → 信號   : {signal_str}{conf_tag}\n"
            f"{'─'*55}"
        )

        return {
            "signal":        signal,
            "high_conf":     high_conf,
            "bull_score":    bull_score,
            "bear_score":    bear_score,
            "adx":           float(curr['adx']),
            "atr":           float(curr['atr']),
            "close":         float(curr['c']),
            "rsi":           float(curr['rsi']),
            "trend_bullish": trend_bullish,
            "bull_exp":      bull_exp,
            "bear_exp":      bear_exp,
            "vol_ok":        vol_ok,
        }

    except Exception as e:
        print(f"⚠️ 指標計算異常: {e}")
        return {
            "signal": 0, "high_conf": False,
            "bull_score": 0, "bear_score": 0,
            "adx": 0.0, "atr": 0.0, "close": 0.0, "rsi": 0.0,
            "trend_bullish": False, "bull_exp": False,
            "bear_exp": False, "vol_ok": False,
        }
