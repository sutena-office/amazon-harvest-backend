import os
import requests
from dotenv import load_dotenv

load_dotenv()
KEEPA_API_KEY = os.getenv("KEEPA_API_KEY")

# プール構築のデフォルト条件（あまかり最適解）
DEFAULT_CRITERIA = {
    "min_price": 10000,    # プリンター実績に合わせた単価。利益額を確保
    "max_price": 60000,    # 資金拘束リスクの上限
    "min_sellers": 3,      # Amazon込み新品出品者数
    "min_rank": 1,         # 対象ランキングの上位側
    "max_rank": 50000,     # 対象ランキングの下位側
    "categories": [],      # 空 = 全カテゴリ（UI側で選択）
}

# カテゴリの優先順位。プリンター実績（型番商品・高単価・消耗品需要・
# 流行り廃りがない）に近い順に並べる。
# S/A は推奨、B は条件付き、C は非推奨（真贋リスク等）、X は転売不可のデジタル商品。
CATEGORY_TIERS = [
    ("S", "最優先", [
        ("パソコン・周辺機器", "プリンター・モニター等。型番商品で一物一価"),
        ("家電&カメラ", "型番商品の王道。単価が高く利益額が出る"),
        ("文房具・オフィス用品", "インク・トナー等の消耗品。法人需要で回転安定"),
        ("ドラッグストア", "消耗品。メーカー保証の概念が薄く新品出品しやすい"),
    ]),
    ("A", "有力", [
        ("ホーム&キッチン", "型番家電が狙い目"),
        ("DIY・工具・ガーデン", "型番商品・単価高・競合が少ない"),
        ("産業・研究開発用品", "法人向け。競合が最も少ない領域"),
        ("ペット用品", "消耗品でリピート需要"),
        ("ベビー&マタニティ", "おむつ等の消耗品需要"),
        ("食品・飲料・お酒", "消耗品。ただし賞味期限に注意"),
        ("大型家電", "単価は最高だが送料・保管リスク大"),
    ]),
    ("B", "条件付き", [
        ("車&バイク", "型番商品だが適合確認が必要"),
        ("楽器・音響機器", "単価は高いが専門知識を要する"),
        ("スポーツ&アウトドア", "ブランド品・サイズ展開に注意"),
        ("おもちゃ", "プレ値狙いは目利き前提"),
        ("ホビー", "同上。相場変動が大きい"),
    ]),
    ("C", "非推奨", [
        ("ビューティー", "ブランド化粧品は真贋調査リスクが極めて高い"),
        ("ファッション", "真贋リスク・サイズ・季節性"),
        ("ゲーム", "値崩れ後に価格が戻らない。転売競合が最多"),
        ("Amazonデバイス・アクセサリ", "Amazon独占。一般セラーは不利"),
        ("本", "単価が低く利益額が出ない"),
        ("洋書", "同上。輸入盤問題もある"),
        ("DVD", "単価が低い。輸入盤リスク"),
        ("ミュージック", "同上"),
        ("デジタルミュージック", "同上"),
    ]),
    ("X", "対象外", [
        ("Kindleストア", "ダウンロード商品のため転売不可"),
        ("PCソフト", "同上"),
        ("Prime Video", "同上"),
        ("アプリ&ゲーム", "同上"),
        ("Alexaスキル", "同上"),
        ("ファイナンス", "同上"),
    ]),
]

# 真贋調査リスクが高く、デフォルトで除外するカテゴリ
COUNTERFEIT_RISK_CATEGORIES = {"ビューティー", "ファッション"}

_category_cache: dict = {}


def _normalize(name: str) -> str:
    """カテゴリ名の表記ゆれ（全角＆・空白）を吸収する"""
    return (name or "").replace("＆", "&").replace(" ", "").replace("　", "").strip()


def _tier_of(name: str) -> tuple:
    """カテゴリ名から (tier, tier_label, 理由) を引く"""
    target = _normalize(name)
    for tier, label, entries in CATEGORY_TIERS:
        for cat_name, reason in entries:
            if _normalize(cat_name) == target:
                return tier, label, reason
    return "B", "条件付き", ""


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
        tier_order = {"S": 0, "A": 1, "B": 2, "C": 3, "X": 4}
        result = []
        for cat_id, cat in cats.items():
            name = cat.get("name", "")
            tier, tier_label, reason = _tier_of(name)
            result.append({
                "id": int(cat_id),
                "name": name,
                "tier": tier,
                "tier_label": tier_label,
                "reason": reason,
                "counterfeit_risk": _normalize(name) in {_normalize(x) for x in COUNTERFEIT_RISK_CATEGORIES},
            })
        result.sort(key=lambda c: (tier_order.get(c["tier"], 9), c["name"]))
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
        "current_SALES_gte": max(1, int(c.get("min_rank") or 1)),
        "current_SALES_lte": int(c["max_rank"]),
        "current_COUNT_NEW_gte": int(c["min_sellers"]),
        "current_NEW_gte": int(c["min_price"]),
        "current_NEW_lte": int(c["max_price"]),
        "perPage": 10000,
        "page": 0,
    }

    exclude_categories = {int(x) for x in (c.get("exclude_categories") or [])}
    if c.get("categories"):
        include = [int(x) for x in c["categories"] if int(x) not in exclude_categories]
        selection["rootCategory"] = include
    elif exclude_categories:
        # 除外指定のみの場合は「全カテゴリ - 除外分」を明示的な含有リストにする
        all_cats = get_root_categories()
        selection["rootCategory"] = [
            cat["id"] for cat in all_cats if cat["id"] not in exclude_categories
        ]

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
