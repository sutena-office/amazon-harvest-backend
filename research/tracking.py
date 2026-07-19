"""
Keepa Tracking API 連携。
watch_listの合格ASINにトラッカーを登録すると、目標価格（中央値×0.77）を
下回った瞬間にKeepaがWebhook（/webhook/keepa）へプッシュ通知してくる。
ポーリング不要・トークン消費最小・即時性最大の監視エンジン。
"""
import os
import requests
from dotenv import load_dotenv
from database import get_client

load_dotenv()
KEEPA_API_KEY = os.getenv("KEEPA_API_KEY")
BACKEND_URL = os.getenv("BACKEND_URL", "https://amazon-harvest-backend.onrender.com")

# 審査バックグラウンドスレッドからも呼ばれるため、専用クライアントを使う
supabase = get_client()

BATCH_SIZE = 100  # 一括登録の単位

# トラッカーの更新間隔（時間）。
# トラッカー維持はトークンフローを毎分削る（実測: 1件あたり毎時約0.5トークン）。
# 1,000件×毎時更新 ≈ -8.3/分 となり5トークン/分プランでは恒常赤字になるため、
# 4時間間隔（≈ -2.1/分）に抑えてプラン内で回るようにする。
TRACKER_UPDATE_INTERVAL_HOURS = 4


def set_webhook_url() -> bool:
    """KeepaアカウントのWebhook URLをこのアプリに設定する"""
    url = "https://api.keepa.com/tracking"
    params = {
        "key": KEEPA_API_KEY,
        "type": "webhook",
        "url": f"{BACKEND_URL}/webhook/keepa",
    }
    try:
        res = requests.get(url, params=params, timeout=15)
        print(f"[TRACKING] webhook設定 status={res.status_code} body={res.text[:200]}", flush=True)
        return res.status_code == 200
    except Exception as e:
        print(f"[TRACKING] webhook設定エラー: {e}", flush=True)
        return False


def add_trackers(items: list) -> dict:
    """
    トラッカーを一括登録する。
    items: [{"asin": ..., "target_price": ...}, ...]
    """
    url = "https://api.keepa.com/tracking"
    params = {"key": KEEPA_API_KEY, "type": "add"}

    tracking_objects = []
    for item in items:
        tracking_objects.append({
            "asin": item["asin"],
            "ttl": 525600,                # 1年間監視（分単位）
            "expireNotify": False,
            "mainDomainId": 5,            # amazon.co.jp
            "updateInterval": TRACKER_UPDATE_INTERVAL_HOURS,
            "metaData": "amazon-harvest",
            "thresholdValues": [
                {
                    "thresholdValue": int(item["target_price"]),
                    "domain": 5,          # amazon.co.jp
                    "csvType": 1,         # 新品最安値
                    "isDrop": True,       # 下回ったら通知
                }
            ],
            # 通知チャネル: [EMAIL, TWITTER, FB通知, BROWSER, FBメッセンジャー, API, モバイル, DUMMY]
            # index 5 = API(Webhook) のみON
            "notificationType": [False, False, False, False, False, True, False, False],
            "individualNotificationInterval": -1,
        })

    try:
        res = requests.post(url, params=params, json=tracking_objects, timeout=60)
        print(f"[TRACKING] 登録 status={res.status_code} 件数={len(tracking_objects)}", flush=True)
        if res.status_code != 200:
            print(f"[TRACKING] エラー: {res.text[:300]}", flush=True)
            return {"ok": False, "error": res.text[:300]}
        data = res.json()
        return {"ok": True, "tokens_left": data.get("tokensLeft", 0)}
    except Exception as e:
        print(f"[TRACKING] 登録例外: {e}", flush=True)
        return {"ok": False, "error": str(e)}


def remove_tracker(asin: str) -> bool:
    url = "https://api.keepa.com/tracking"
    params = {"key": KEEPA_API_KEY, "type": "remove", "asin": asin}
    try:
        res = requests.get(url, params=params, timeout=15)
        return res.status_code == 200
    except Exception:
        return False


def register_trackers_for_user(user_id: str) -> dict:
    """watch_listの承認済みASINをまとめてトラッカー登録し、statusをtrackingに更新"""
    set_webhook_url()

    # approved(未登録)だけでなくtracking(登録済み)も対象に含める。
    # 同一ASINの再登録はKeepa側で上書きになるため、
    # updateInterval等の設定変更を既存トラッカーへ一括反映できる。
    res = (
        supabase.table("watch_list")
        .select("asin, target_price")
        .eq("user_id", user_id)
        .in_("status", ["approved", "tracking"])
        .execute()
    )
    rows = res.data or []
    if not rows:
        print("[TRACKING] 登録対象なし", flush=True)
        return {"registered": 0}

    registered = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        result = add_trackers(batch)
        if result.get("ok"):
            asins = [r["asin"] for r in batch]
            supabase.table("watch_list").update({"status": "tracking"}).eq(
                "user_id", user_id
            ).in_("asin", asins).execute()
            registered += len(batch)
        else:
            print(f"[TRACKING] バッチ{i // BATCH_SIZE}失敗、中断", flush=True)
            break

    print(f"[TRACKING] トラッカー登録完了: {registered}/{len(rows)}件", flush=True)
    return {"registered": registered, "total": len(rows)}
