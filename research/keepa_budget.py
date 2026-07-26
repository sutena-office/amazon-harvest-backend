"""
Keepaのトークン予算を実測して、各処理のペースを自動調整する。

プランを増強しても設定値がハードコードのままだと速度が上がらず、
逆にプランが小さいのに攻めた設定にすると恒常的なトークン赤字で
全APIが429になる（実際に発生した）。
そのため refillRate（補充/分）と tokenFlowReduction（トラッカー維持費/分）を
実測し、そこから安全なペースを毎回導出する。
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()
KEEPA_API_KEY = os.getenv("KEEPA_API_KEY")

_FALLBACK = {"refill_rate": 5.0, "flow_reduction": 0.0, "tokens_left": 0}


def get_token_status() -> dict:
    """トークンの補充レートと維持費を取得する（消費トークンなし）"""
    if not KEEPA_API_KEY:
        return dict(_FALLBACK)
    try:
        res = requests.get(
            "https://api.keepa.com/token",
            params={"key": KEEPA_API_KEY},
            timeout=15,
        )
        if res.status_code != 200:
            return dict(_FALLBACK)
        d = res.json()
        status = {
            "refill_rate": float(d.get("refillRate") or 5),
            "flow_reduction": float(d.get("tokenFlowReduction") or 0),
            "tokens_left": int(d.get("tokensLeft") or 0),
        }
        print(
            f"[BUDGET] 補充={status['refill_rate']}/分 "
            f"維持費={status['flow_reduction']:.2f}/分 "
            f"残={status['tokens_left']}",
            flush=True,
        )
        return status
    except Exception as e:
        print(f"[BUDGET] 取得エラー: {e}", flush=True)
        return dict(_FALLBACK)


# Webhook処理など他用途のために常に残しておくトークン
TOKEN_RESERVE = 50


def screening_pace_seconds(item_count: int = None) -> int:
    """審査1件あたりの待機秒数。

    補充レートだけでなく「手持ちのトークン残高」も考慮する。
    残高で全件まかなえるなら待つ必要がないため大幅に速く回せる
    （例: 残299・214件 → 補充待ちゼロで最速）。
    """
    s = get_token_status()
    spare = s["refill_rate"] - s["flow_reduction"]
    usable = max(0.5, spare * 0.9)  # 1割は他処理用に残す

    if item_count and item_count > 0:
        available = max(0, s["tokens_left"] - TOKEN_RESERVE)
        # 手持ちで足りない分だけを補充待ちする
        needed_from_refill = max(0, item_count - available)
        min_seconds = (needed_from_refill / usable) * 60
        pace = int(min_seconds / item_count)
    else:
        pace = int(60 / usable)

    pace = max(3, min(pace, 60))  # 3〜60秒に収める
    print(
        f"[BUDGET] 審査ペース: {pace}秒/件"
        f"（残{s['tokens_left']} / 対象{item_count or '?'}件）",
        flush=True,
    )
    return pace


def tracker_update_interval_hours() -> int:
    """トラッカーの価格チェック間隔。補充レートが大きいほど短くできる。"""
    rate = get_token_status()["refill_rate"]
    if rate >= 20:
        interval = 1
    elif rate >= 10:
        interval = 2
    else:
        interval = 4
    print(f"[BUDGET] トラッカー更新間隔: {interval}時間", flush=True)
    return interval


def deals_scan_interval_hours() -> int:
    """Dealsスキャン（新規発掘）の実行間隔。"""
    rate = get_token_status()["refill_rate"]
    if rate >= 20:
        return 1
    elif rate >= 10:
        return 2
    return 3
