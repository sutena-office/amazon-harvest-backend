import os
import requests
from dotenv import load_dotenv

load_dotenv()
KEEPA_API_KEY = os.getenv("KEEPA_API_KEY")

# プール構築のデフォルト条件（あまかり最適解）
DEFAULT_CRITERIA = {
    "min_price": 5000,     # 利益500円が現実的に出る下限
    "max_price": 50000,    # 資金拘束リスクの上限
    "min_sellers": 3,      # Amazon込み新品出品者数
    "max_rank": 50000,     # 売れ筋上位のみ
    "categories": [],      # 空 = 全カテゴリ（UI側で選択）
}

_category_cache: dict = {}


def get_root_categories() -> list:
    """日本Amazonのルートカテゴリ一覧を取得（結果はメモリキャッシュ）"""
    global _category_cache
    if _category_cache:
        return list(_category_cache.values())

    url = "https://api.keepa.com/category"
    params = {"key": KEEPA_API_KEY, "domain": 5, "category": 0, "parents": 0}
    try:
        res = requests.get(url, params=params, timeout=15)
        print(f"[POOL] カテゴリ取得 status={res.status_code}", flush=True)
        if res.status_code != 200:
            return []
        data = res.json()
        cats = data.get("categories") or {}
        result = []
        for cat_id, cat in cats.items():
            result.append({"id": int(cat_id), "name": cat.get("name", "")})
        result.sort(key=lambda c: c["name"])
        _category_cache = {c["id"]: c for c in result}
        return result
    except Exception as e:
        print(f"[POOL] カテゴリ取得エラー: {e}", flush=True)
        return []


def find_pool_asins(criteria: dict) -> dict:
    """
    Product Finder API (/query) で条件に合うASINリストを取得する。
    Keepaのデータベース全体から検索するため、事前の商品リストは不要。
    """
    if not KEEPA_API_KEY:
        return {"asins": [], "total": 0, "error": "APIキー未設定"}

    c = {**DEFAULT_CRITERIA, **{k: v for k, v in criteria.items() if v is not None}}

    selection = {
        "current_SALES_gte": 1,
        "current_SALES_lte": int(c["max_rank"]),
        "current_COUNT_NEW_gte": int(c["min_sellers"]),
        "current_NEW_gte": int(c["min_price"]),
        "current_NEW_lte": int(c["max_price"]),
        "perPage": 10000,
        "page": 0,
    }
    if c.get("categories"):
        selection["rootCategory"] = [int(x) for x in c["categories"]]

    url = "https://api.keepa.com/query"
    try:
        res = requests.post(
            url,
            params={"key": KEEPA_API_KEY, "domain": 5},
            json=selection,
            timeout=60,
        )
        print(f"[POOL] Product Finder status={res.status_code}", flush=True)
        if res.status_code != 200:
            print(f"[POOL] エラー: {res.text[:300]}", flush=True)
            return {"asins": [], "total": 0, "error": res.text[:200]}

        data = res.json()
        asins = data.get("asinList") or []
        total = data.get("totalResults", len(asins))
        tokens_left = data.get("tokensLeft", 0)
        print(f"[POOL] 該当{total}件 取得{len(asins)}件 トークン残={tokens_left}", flush=True)
        return {"asins": asins, "total": total, "tokens_left": tokens_left}

    except Exception as e:
        print(f"[POOL] Product Finder例外: {e}", flush=True)
        return {"asins": [], "total": 0, "error": str(e)}
