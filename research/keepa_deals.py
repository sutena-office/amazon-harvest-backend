import os
import requests
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

KEEPA_API_KEY = os.getenv("KEEPA_API_KEY")


def get_keepa_deals(
    min_drop_percent: float = 20.0,
    max_rank: int = 100000,
    date_range: int = 2,
    page: int = 0,
) -> list:
    """
    Keepa Deals APIで値下がり商品を取得する。
    date_range: 1=24時間以内, 2=48時間以内, 3=1週間以内
    """
    if not KEEPA_API_KEY:
        print("[KEEPA_DEALS] APIキー未設定", flush=True)
        return []

    url = "https://api.keepa.com/deal"
    params = {
        "key": KEEPA_API_KEY,
        "domainId": 5,          # amazon.co.jp
        "page": page,
        "priceTypes": 0,        # Amazon本体価格
        "deltaPercent": int(-abs(min_drop_percent)),  # 負の値=値下がり率
        "dateRange": date_range,
    }
    if max_rank > 0:
        params["salesRankRange"] = f"1,{max_rank}"

    try:
        response = requests.get(url, params=params, timeout=30)
        print(f"[KEEPA_DEALS] status={response.status_code}", flush=True)

        if response.status_code != 200:
            print(f"[KEEPA_DEALS] エラー: {response.text[:300]}", flush=True)
            return []

        data = response.json()
        tokens_left = data.get("tokensLeft", "不明")
        print(f"[KEEPA_DEALS] トークン残量: {tokens_left}", flush=True)

        deals_data = data.get("deals", {})
        if isinstance(deals_data, dict):
            dr = deals_data.get("dr", [])
        else:
            dr = []

        print(f"[KEEPA_DEALS] {len(dr)}件取得", flush=True)
        return dr

    except Exception as e:
        print(f"[KEEPA_DEALS] 例外: {e}", flush=True)
        return []


def parse_deal(deal: dict) -> Optional[dict]:
    """
    ディールデータを解析して刈り取り候補情報を返す。
    通常価格として90日平均を使用し、現在価格との差額で利益を計算する。
    """
    asin = deal.get("asin", "")
    if not asin:
        return None

    title = (deal.get("title") or "").strip()

    # 現在価格（値下がり後）
    current = deal.get("current") or []
    current_price = _get_price(current, 0)

    # 通常価格の推定: 90日平均 > 180日平均 の順で参照
    avg90 = deal.get("avg90") or []
    avg180 = deal.get("avg180") or []
    avg90_price = _get_price(avg90, 0)
    avg180_price = _get_price(avg180, 0)

    # 通常価格 = 90日平均と180日平均の高い方（より保守的な見積もり）
    regular_price = max(avg90_price, avg180_price)

    if not current_price or current_price <= 0:
        print(f"[KEEPA_DEALS] スキップ（現在価格なし）: {asin}", flush=True)
        return None
    if not regular_price or regular_price <= current_price:
        print(f"[KEEPA_DEALS] スキップ（通常価格≤現在価格）: {asin} 現在={current_price} 通常={regular_price}", flush=True)
        return None

    drop_rate = (regular_price - current_price) / regular_price * 100
    sales_rank = deal.get("salesRank") or 0
    root_category = deal.get("rootCategory") or 0

    print(
        f"[KEEPA_DEALS] '{title[:25]}' ASIN={asin} "
        f"現在={current_price}円 通常={regular_price}円 "
        f"値下がり={round(drop_rate, 1)}% ランク={sales_rank}位",
        flush=True,
    )

    return {
        "asin": asin,
        "product_name": title,
        "current_price": current_price,   # 仕入れ値（今すぐ買える価格）
        "regular_price": regular_price,   # 通常価格（転売目標価格）
        "price_drop_rate": round(drop_rate, 1),
        "amazon_rank": sales_rank,
        "root_category": root_category,
    }


def get_keepa_data_by_asin(asin: str) -> Optional[dict]:
    """
    KeepaのWebhookで受信したASINの詳細情報を取得する。
    (Keepa Webhook受信時のリアルタイム処理用)
    """
    if not KEEPA_API_KEY or not asin:
        return None

    url = "https://api.keepa.com/product"
    params = {
        "key": KEEPA_API_KEY,
        "domain": 5,
        "asin": asin,
        "stats": 90,
    }

    try:
        response = requests.get(url, params=params, timeout=15)
        if response.status_code != 200:
            return None

        data = response.json()
        products = data.get("products", [])
        if not products:
            return None

        product = products[0]
        title = (product.get("title") or "").strip()
        csv = product.get("csv") or []

        current_price = _get_current_price_from_csv(csv, 0)   # Amazon本体
        if not current_price:
            current_price = _get_current_price_from_csv(csv, 18)  # BuyBox

        # 90日・180日平均
        stats = product.get("stats") or {}
        avg90_price = _get_stats_avg(stats, "avg", 0)
        avg180_price = _get_stats_avg(stats, "avg", 0, days=180)
        regular_price = max(avg90_price, avg180_price)

        if not current_price or not regular_price or current_price >= regular_price:
            return None

        drop_rate = (regular_price - current_price) / regular_price * 100

        # ランキング
        rank = 0
        if len(csv) > 3 and csv[3] and len(csv[3]) >= 2:
            rank = csv[3][-1] or 0

        print(
            f"[KEEPA_ASIN] '{title[:25]}' ASIN={asin} "
            f"現在={current_price}円 通常={regular_price}円 値下がり={round(drop_rate, 1)}%",
            flush=True,
        )

        return {
            "asin": asin,
            "product_name": title,
            "current_price": current_price,
            "regular_price": regular_price,
            "price_drop_rate": round(drop_rate, 1),
            "amazon_rank": rank,
        }

    except Exception as e:
        print(f"[KEEPA_ASIN] 例外: {e}", flush=True)
        return None


def _get_current_price_from_csv(csv: list, index: int) -> int:
    """CSV配列からindex番目の現在価格を取得"""
    if not csv or len(csv) <= index:
        return 0
    data = csv[index]
    if not data or len(data) < 2:
        return 0
    price = data[-1]
    if price and price > 0:
        return int(price)
    return 0


def _get_stats_avg(stats: dict, key: str, index: int, days: int = 90) -> int:
    """statsからavg価格を取得"""
    avg_key = f"avg{days}" if days != 90 else "avg"
    avg = stats.get(avg_key) or []
    if avg and len(avg) > index:
        v = avg[index]
        if v and v > 0:
            return int(v)
    return 0


def _get_price(price_array: list, index: int) -> int:
    """価格配列からindex番目の価格を取得（JPY=直接の円単位）"""
    if not price_array or len(price_array) <= index:
        return 0
    price = price_array[index]
    if price and price > 0:
        return int(price)
    return 0
