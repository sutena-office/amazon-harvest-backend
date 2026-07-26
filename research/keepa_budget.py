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


def screening_pace_seconds() -> int:
    """審査1件あたりの待機秒数。
    トラッカー維持費を差し引いた実質の余剰トークンから算出する。"""
    s = get_token_status()
    spare = s["refill_rate"] - s["flow_reduction"]
    usable = max(0.5, spare * 0.9)  # 1割は他処理用に残す
    pace = int(60 / usable)
    pace = max(3, min(pace, 60))  # 3〜60秒に収める
    print(f"[BUDGET] 審査ペース: {pace}秒/件", flush=True)
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
