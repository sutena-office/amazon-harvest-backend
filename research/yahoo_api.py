"""
Yahoo!ショッピング 商品検索API v3 連携。

Keepaと違いトークン制限がなく（1リクエスト/秒の目安のみ）、無料で使える。
Yahoo!側の「ポイント込み実質価格」を取得するのがこのモジュールの役割。
利用にはYahoo!デベロッパーネットワークのClient ID（環境変数 YAHOO_CLIENT_ID）が必要。
https://e.developer.yahoo.co.jp/register
"""
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()
YAHOO_CLIENT_ID = os.getenv("YAHOO_CLIENT_ID")

API_URL = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
PACE_SECONDS = 1.1  # 目安1リクエスト/秒を守る


def is_configured() -> bool:
    return bool(YAHOO_CLIENT_ID)


# PayPay基本付与率（%）。PayPay/PayPayカード払いの標準還元。
# APIレスポンスには含まれないため率で加算する
BASE_POINT_RATE = float(os.getenv("YAHOO_BASE_POINT_RATE", "1.0"))
# LYPプレミアム会員として計算するか（会員なら店舗ポイントが優遇される）
USE_PREMIUM = os.getenv("YAHOO_USE_PREMIUM", "true").lower() == "true"


def _effective_price(hit: dict) -> dict:
    """ポイント還元を差し引いた実質価格を計算する。

    重要: point.amount / bonusAmount / premiumAmount 系は
    Yahoo!側の仕様変更により恒久的に0が返る（2022年4月・2025年2月）。
    現在ストア独自ポイントが入るのは lyLimited* 系のみ。
    さらにPayPay基本付与・5のつく日等のキャンペーン分はAPIに含まれないため、
    基本付与は率で加算し、キャンペーン分は呼び出し側でboostとして上乗せする。
    """
    price = int(hit.get("price") or 0)
    point = hit.get("point") or {}

    if USE_PREMIUM:
        store_point = int(
            point.get("lyLimitedPremiumBonusAmount")
            or point.get("lyLimitedBonusAmount")
            or 0
        )
    else:
        store_point = int(point.get("lyLimitedBonusAmount") or 0)

    base_point = int(price * BASE_POINT_RATE / 100)

    coupon_amount = 0
    coupon = hit.get("coupon") or {}
    if isinstance(coupon, dict):
        coupon_amount = int(coupon.get("discount") or 0)

    total_point = store_point + base_point
    effective = price - total_point - coupon_amount
    return {
        "price": price,
        "point": total_point,
        "store_point": store_point,
        "base_point": base_point,
        "coupon": coupon_amount,
        "effective": effective,
    }


def search_best_by_jan(jan: str) -> dict | None:
    """JANコードで新品在庫ありを検索し、実質価格が最安の1件を返す（無料）"""
    if not YAHOO_CLIENT_ID or not jan:
        return None
    params = {
        "appid": YAHOO_CLIENT_ID,
        "jan_code": jan,
        "condition": "new",
        "in_stock": "true",
        "results": 20,
        "sort": "+price",
    }
    try:
        res = requests.get(API_URL, params=params, timeout=15)
        if res.status_code == 429:
            time.sleep(3)
            res = requests.get(API_URL, params=params, timeout=15)
        if res.status_code != 200:
            print(f"[YAHOO] JAN={jan} status={res.status_code} {res.text[:150]}", flush=True)
            return None

        hits = (res.json() or {}).get("hits") or []
        if not hits:
            return None

        best = None
        for hit in hits:
            ep = _effective_price(hit)
            if ep["price"] <= 0:
                continue
            candidate = {
                **ep,
                "name": (hit.get("name") or "")[:200],
                "url": hit.get("url") or "",
                "store": ((hit.get("seller") or {}).get("name") or "")[:100],
            }
            if best is None or candidate["effective"] < best["effective"]:
                best = candidate
        return best
    except Exception as e:
        print(f"[YAHOO] JAN={jan} 例外: {e}", flush=True)
        return None
    finally:
        time.sleep(PACE_SECONDS)
