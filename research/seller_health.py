import os
import requests
from dotenv import load_dotenv

load_dotenv()
KEEPA_API_KEY = os.getenv("KEEPA_API_KEY")

# 日本Amazonの主要カテゴリ別商品数（概算）
# 上位3%の閾値計算に使用
CATEGORY_SIZES = {
    14304371:   15000,    # ゲーム
    2016926051: 500000,   # おもちゃ＆ホビー
    13900771:   2000000,  # エレクトロニクス
    281055:     3000000,  # 本
    2277721051: 800000,   # スポーツ＆アウトドア
    3210981:    600000,   # ヘルス＆ビューティー
    2250738051: 400000,   # ホーム＆キッチン
    0:          1000000,  # 不明カテゴリのデフォルト
}


def check_seller_health(asin: str, root_category: int = 0) -> dict:
    """
    Keepa Product APIでセラー健全性を4条件チェックする。
    - 過去90日 平均出品者数 ≥ 3名
    - 直近30日 出品者数の急減が50%未満
    - Amazon独占疑いなし（出品者5名未満 + Amazon常時販売は除外）
    - 売れ筋ランキング カテゴリ上位3%以内

    Returns: {"healthy": bool, "reason": str, ...stats}
    """
    if not KEEPA_API_KEY:
        return {"healthy": True, "reason": "APIキー未設定（スキップ）"}

    url = "https://api.keepa.com/product"
    params = {
        "key": KEEPA_API_KEY,
        "domain": 5,
        "asin": asin,
        "stats": 90,
    }

    try:
        res = requests.get(url, params=params, timeout=15)
        print(f"[HEALTH] {asin} status={res.status_code}", flush=True)
        if res.status_code != 200:
            return {"healthy": True, "reason": f"APIエラー{res.status_code}（スキップ）"}

        data = res.json()
        print(f"[HEALTH] トークン残量: {data.get('tokensLeft', '不明')}", flush=True)

        products = data.get("products", [])
        if not products:
            return {"healthy": False, "reason": "商品データなし"}

        stats = products[0].get("stats") or {}
        # Keepa stats: "avg" = 30日平均, "avg90" = 90日平均
        avg90 = stats.get("avg90") or []
        avg30 = stats.get("avg") or []

        # ① 過去90日 平均出品者数 ≥ 3名 (CSV index 11 = New Offer Count)
        seller_90 = _val(avg90, 11)
        if seller_90 < 3:
            print(f"[HEALTH] {asin} NG: 出品者不足(90日avg={seller_90}人)", flush=True)
            return {"healthy": False, "reason": f"出品者不足(90日avg:{seller_90}人)"}

        # ② 直近30日で出品者数が50%以上急減していない
        seller_30 = _val(avg30, 11)
        if seller_30 > 0 and seller_30 < seller_90 * 0.5:
            print(f"[HEALTH] {asin} NG: 出品者急減({seller_90}→{seller_30}人)", flush=True)
            return {"healthy": False, "reason": f"出品者急減({seller_90}→{seller_30}人)"}

        # ③ Amazon独占疑い排除
        # Amazon価格が90日間有効 かつ 出品者が5人未満 = 一般セラーがカートを取れない可能性大
        amazon_price_90 = _val(avg90, 0)
        if amazon_price_90 > 0 and seller_90 < 5:
            print(f"[HEALTH] {asin} NG: Amazon独占疑い(出品者{seller_90}人)", flush=True)
            return {"healthy": False, "reason": f"Amazon独占疑い(出品者{seller_90}人)"}

        # ④ 売れ筋ランキング カテゴリ上位3%以内 (CSV index 3 = Sales Rank)
        rank_90 = _val(avg90, 3)
        if rank_90 > 0:
            cat_size = CATEGORY_SIZES.get(root_category, 1000000)
            threshold = int(cat_size * 0.03)
            if rank_90 > threshold:
                print(f"[HEALTH] {asin} NG: ランク低({rank_90:,}位 > 上位3%={threshold:,}位)", flush=True)
                return {"healthy": False, "reason": f"ランク低(90日avg:{rank_90:,}位)"}

        print(
            f"[HEALTH] {asin} OK: 出品者90日avg={seller_90}人 30日avg={seller_30}人 "
            f"ランク90日avg={rank_90:,}位",
            flush=True,
        )
        return {
            "healthy": True,
            "reason": "OK",
            "seller_count_avg90": seller_90,
            "seller_count_avg30": seller_30,
            "rank_avg90": rank_90,
        }

    except Exception as e:
        print(f"[HEALTH] {asin} 例外: {e}", flush=True)
        return {"healthy": True, "reason": f"チェック失敗（スキップ）"}


def _val(arr: list, index: int) -> int:
    """配列からindex番目の値を取得（なければ0）"""
    if not arr or len(arr) <= index:
        return 0
    v = arr[index]
    return int(v) if v and v > 0 else 0
