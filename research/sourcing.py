"""
Yahoo!仕入れモード（種→派生方式）。

成功実績のあるモデル商品（例: プリンター）のASINを「種」として登録すると、
1. Keepaから種の特徴（カテゴリ・ブランド・価格帯・JAN）を抽出
2. Product Finderで「同じ勝ち筋」の類似商品を発掘
3. 各候補のJANでYahoo!ショッピングを検索し、ポイント込み実質価格を取得
4. Amazon想定売価（90日平均・経費18%控除）との差で利益を判定
という流れで「プリンターに準ずる商品」を自動リストアップする。

あまかり（Amazon内の急落待ち）とは独立した機能。
Yahoo!側のチェックは無料なので毎日全件再スキャンできる。
"""
import os
import time
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from database import get_client
from research.yahoo_api import search_best_by_jan, is_configured

load_dotenv()
KEEPA_API_KEY = os.getenv("KEEPA_API_KEY")

supabase = get_client()

FEE_RATE = 0.18          # Amazon手数料+送料の経費率
MAX_CANDIDATES = 300     # 種1つあたりの候補上限（Keepaトークン節約）


def _keepa_product(asin: str, with_history: bool = False) -> dict | None:
    """Keepaから商品情報を1件取得（1トークン）"""
    params = {
        "key": KEEPA_API_KEY,
        "domain": 5,
        "asin": asin,
        "stats": 90,
        "history": 1 if with_history else 0,
    }
    try:
        res = requests.get("https://api.keepa.com/product", params=params, timeout=20)
        if res.status_code == 429:
            return {"retry": True}
        if res.status_code != 200:
            return None
        products = (res.json() or {}).get("products") or []
        return products[0] if products else None
    except Exception as e:
        print(f"[SOURCING] Keepa取得例外 {asin}: {e}", flush=True)
        return None


def _stat(p: dict, key: str, idx: int) -> int:
    arr = (p.get("stats") or {}).get(key) or []
    if len(arr) > idx and arr[idx] and arr[idx] > 0:
        return int(arr[idx])
    return 0


def analyze_seed(asin: str) -> dict | None:
    """種商品の特徴を抽出する（1トークン）"""
    p = _keepa_product(asin)
    if not p or p.get("retry"):
        return None
    title = (p.get("title") or "").strip()
    brand = (p.get("brand") or "").strip()
    root_category = p.get("rootCategory") or 0
    ean_list = p.get("eanList") or []
    price = _stat(p, "avg90", 1) or _stat(p, "current", 1) or _stat(p, "avg90", 0)
    if not price:
        return None
    return {
        "asin": asin,
        "title": title,
        "brand": brand,
        "root_category": root_category,
        "jan": ean_list[0] if ean_list else "",
        "reference_price": price,
    }


def discover_similar_asins(traits: dict) -> list:
    """Product Finderで種に似た商品のASINを探す（同カテゴリ×同ブランド×近い価格帯）"""
    price = traits["reference_price"]
    selection = {
        "rootCategory": [int(traits["root_category"])],
        "current_NEW_gte": int(price * 0.4),
        "current_NEW_lte": int(price * 2.5),
        "current_COUNT_NEW_gte": 3,
        "current_SALES_gte": 1,
        "current_SALES_lte": 100000,
        "perPage": MAX_CANDIDATES,
        "page": 0,
    }
    if traits.get("brand"):
        selection["brand"] = [traits["brand"]]

    try:
        res = requests.post(
            "https://api.keepa.com/query",
            params={"key": KEEPA_API_KEY, "domain": 5},
            json=selection,
            timeout=60,
        )
        if res.status_code != 200:
            print(f"[SOURCING] Product Finder status={res.status_code} {res.text[:200]}", flush=True)
            return []
        data = res.json()
        asins = data.get("asinList") or []
        print(f"[SOURCING] 類似候補 {len(asins)}件（ブランド={traits.get('brand') or '指定なし'}）", flush=True)

        # ブランド一致で少なすぎる場合はブランド条件を外して再検索
        if len(asins) < 30 and traits.get("brand"):
            selection.pop("brand", None)
            res2 = requests.post(
                "https://api.keepa.com/query",
                params={"key": KEEPA_API_KEY, "domain": 5},
                json=selection,
                timeout=60,
            )
            if res2.status_code == 200:
                extra = (res2.json() or {}).get("asinList") or []
                asins = list(dict.fromkeys(asins + extra))[:MAX_CANDIDATES]
                print(f"[SOURCING] ブランド外拡張後 {len(asins)}件", flush=True)
        return asins
    except Exception as e:
        print(f"[SOURCING] Product Finder例外: {e}", flush=True)
        return []


def _campaign_bonus(price: int, boost_percent: float, boost_cap: int) -> int:
    """キャンペーン還元の想定額。5のつく日等は付与上限があるためcapで頭打ちにする"""
    if boost_percent <= 0:
        return 0
    bonus = int(price * boost_percent / 100)
    return min(bonus, boost_cap) if boost_cap > 0 else bonus


def _profit_calc(amazon_price: int, yahoo_effective: int,
                 boost_percent: float = 0, boost_cap: int = 0,
                 yahoo_price: int = 0) -> dict:
    """利益計算。boostはキャンペーン日（5のつく日+4%等）の追加還元想定"""
    bonus = _campaign_bonus(yahoo_price or yahoo_effective, boost_percent, boost_cap)
    cost = yahoo_effective - bonus
    sell_net = int(amazon_price * (1 - FEE_RATE))
    profit = sell_net - cost
    rate = round(profit / amazon_price * 100, 1) if amazon_price else 0
    return {"profit_amount": profit, "profit_rate": rate, "campaign_bonus": bonus}


def evaluate_candidate(asin: str) -> dict | None:
    """候補1件を評価: Keepaで売価・月販目安・JAN→Yahoo!で実質仕入れ値→利益計算"""
    p = _keepa_product(asin)
    if not p:
        return None
    if p.get("retry"):
        return {"retry": True}

    title = (p.get("title") or "").strip()
    ean_list = p.get("eanList") or []
    if not ean_list:
        return None  # JANがないとYahoo!照合不可

    amazon_price = _stat(p, "avg90", 1) or _stat(p, "current", 1)
    if not amazon_price:
        return None
    rank = _stat(p, "current", 3) or _stat(p, "avg90", 3)

    # 月販目安: 30日間のランキング急落回数（Keepaの販売数代理指標）
    stats = p.get("stats") or {}
    est_monthly_sales = int(stats.get("salesRankDrops30") or 0)
    seller_count = _stat(p, "current", 11) or _stat(p, "avg90", 11)

    yahoo = search_best_by_jan(ean_list[0])
    if not yahoo:
        return None

    pr = _profit_calc(amazon_price, yahoo["effective"], yahoo_price=yahoo["price"])
    # 月間期待利益 = 利益 × 月販目安（プロが商品を選ぶ本当の物差し）
    expected_monthly = pr["profit_amount"] * est_monthly_sales if pr["profit_amount"] > 0 else 0
    # 講座生1人あたりの現実的な月間利益 = 月販を出品者数+1で分け合った場合の取り分
    student_monthly = _student_share(pr["profit_amount"], est_monthly_sales, seller_count)

    return {
        "asin": asin,
        "jan": ean_list[0],
        "product_name": title,
        "amazon_price": amazon_price,
        "amazon_rank": rank,
        "est_monthly_sales": est_monthly_sales,
        "seller_count": seller_count,
        "yahoo_price": yahoo["price"],
        "yahoo_point": yahoo["point"],
        "yahoo_effective": yahoo["effective"],
        "yahoo_url": yahoo["url"],
        "yahoo_store": yahoo["store"],
        "profit_amount": pr["profit_amount"],
        "profit_rate": pr["profit_rate"],
        "expected_monthly_profit": expected_monthly,
        "student_monthly_profit": student_monthly,
    }


def _student_share(profit: int, monthly_sales: int, seller_count: int) -> int:
    """講座生1人が新規参入した場合の現実的な月間利益。
    月販を「既存出品者+自分」で均等に分け合う想定（カート取得の簡易モデル）"""
    if profit <= 0 or monthly_sales <= 0:
        return 0
    share = monthly_sales / max(seller_count + 1, 1)
    return int(profit * share)


def run_sourcing_job(seed_id: str, user_id: str, traits: dict):
    """種→派生の発掘バッチ（バックグラウンドスレッドで実行）"""
    from research.keepa_budget import screening_pace_seconds
    pace = screening_pace_seconds()

    asins = discover_similar_asins(traits)
    if not asins:
        supabase.table("sourcing_seeds").update(
            {"status": "done", "total": 0, "checked": 0, "hits": 0}
        ).eq("id", seed_id).execute()
        return

    supabase.table("sourcing_seeds").update({"total": len(asins)}).eq("id", seed_id).execute()
    print(f"[SOURCING] 発掘開始 seed={seed_id} 候補={len(asins)}件 ペース={pace}秒/件", flush=True)

    checked = 0
    hits = 0
    for asin in asins:
        result = evaluate_candidate(asin)
        if result and result.get("retry"):
            print("[SOURCING] トークン待ち120秒", flush=True)
            time.sleep(120)
            result = evaluate_candidate(asin)

        checked += 1
        if result and not result.get("retry"):
            try:
                supabase.table("sourcing_candidates").upsert(
                    {
                        "user_id": user_id,
                        "seed_id": seed_id,
                        **result,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    on_conflict="user_id,asin",
                ).execute()
                if result["profit_amount"] > 0:
                    hits += 1
            except Exception as e:
                print(f"[SOURCING] DB保存エラー {asin}: {e}", flush=True)

        if checked % 10 == 0 or checked == len(asins):
            supabase.table("sourcing_seeds").update(
                {"checked": checked, "hits": hits}
            ).eq("id", seed_id).execute()
            print(f"[SOURCING] 進捗 {checked}/{len(asins)} 利益あり{hits}件", flush=True)

        time.sleep(pace)

    supabase.table("sourcing_seeds").update(
        {"status": "done", "checked": checked, "hits": hits,
         "finished_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", seed_id).execute()
    print(f"[SOURCING] 発掘完了 seed={seed_id} 利益あり{hits}/{checked}件", flush=True)


def rescan_yahoo_prices(user_id: str = None, notify: bool = False,
                        boost_percent: float = None, boost_cap: int = None) -> dict:
    """既存候補のYahoo!実質価格を再チェックする（無料・毎日実行可）。
    Keepaは使わないのでトークン消費ゼロ。

    boost未指定なら「今日の日付から自動判定したキャンペーン還元」を適用する
    （5のつく日・日曜・会員特典）。明示指定すればその条件でシミュレーションできる。"""
    if not is_configured():
        return {"checked": 0, "error": "YAHOO_CLIENT_ID未設定"}

    if boost_percent is None:
        from research.yahoo_points import campaign_for_date
        camp = campaign_for_date()
        boost_percent = camp["rate"]
        boost_cap = camp["cap"]
        print(f"[SOURCING] 本日の還元を自動適用: {camp['summary']}", flush=True)
    boost_cap = boost_cap or 0

    query = supabase.table("sourcing_candidates").select("*")
    if user_id:
        query = query.eq("user_id", user_id)
    rows = (query.execute()).data or []

    checked = 0
    improved = []
    for row in rows:
        yahoo = search_best_by_jan(row["jan"])
        if not yahoo:
            continue
        pr = _profit_calc(
            row["amazon_price"], yahoo["effective"],
            boost_percent=boost_percent, boost_cap=boost_cap,
            yahoo_price=yahoo["price"],
        )
        est_sales = int(row.get("est_monthly_sales") or 0)
        expected_monthly = pr["profit_amount"] * est_sales if pr["profit_amount"] > 0 else 0
        student_monthly = _student_share(
            pr["profit_amount"], est_sales, int(row.get("seller_count") or 0)
        )

        supabase.table("sourcing_candidates").update(
            {
                "yahoo_price": yahoo["price"],
                "yahoo_point": yahoo["point"],
                "yahoo_effective": yahoo["effective"] - pr["campaign_bonus"],
                "yahoo_url": yahoo["url"],
                "yahoo_store": yahoo["store"],
                "profit_amount": pr["profit_amount"],
                "profit_rate": pr["profit_rate"],
                "expected_monthly_profit": expected_monthly,
                "student_monthly_profit": student_monthly,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", row["id"]).execute()
        checked += 1

        # 利益が新たに閾値を超えたものを通知対象に
        if notify and pr["profit_amount"] >= 1000 and (row.get("profit_amount") or 0) < 1000:
            improved.append({
                "product_name": row["product_name"],
                "profit_amount": pr["profit_amount"],
                "yahoo_effective": yahoo["effective"] - pr["campaign_bonus"],
                "amazon_price": row["amazon_price"],
                "yahoo_url": yahoo["url"],
                "asin": row["asin"],
            })

    if notify and improved:
        _notify_finds(improved)

    print(f"[SOURCING] Yahoo!再スキャン完了 {checked}件 新規利益{len(improved)}件 boost={boost_percent}%", flush=True)
    return {"checked": checked, "new_finds": len(improved)}


def _notify_finds(finds: list):
    """Yahoo!仕入れシグナルをDiscordへ通知"""
    try:
        res = supabase.table("harvest_settings").select("*").eq("notify_enabled", True).execute()
        for setting in res.data or []:
            webhook = setting.get("discord_webhook_url")
            if not webhook:
                continue
            for f in finds[:10]:
                payload = {
                    "username": "🛒 Yahoo!仕入れbot",
                    "embeds": [{
                        "title": f"💰 {f['product_name'][:100]}",
                        "url": f["yahoo_url"],
                        "color": 0x2E86DE,
                        "fields": [
                            {"name": "🛒 Yahoo!実質仕入れ値", "value": f"**¥{f['yahoo_effective']:,}**（ポイント込み）", "inline": True},
                            {"name": "💴 Amazon売価(90日平均)", "value": f"¥{f['amazon_price']:,}", "inline": True},
                            {"name": "📈 見込み利益(経費18%後)", "value": f"**¥{f['profit_amount']:,}**", "inline": True},
                            {"name": "🔗 Amazon", "value": f"[商品ページ](https://www.amazon.co.jp/dp/{f['asin']})", "inline": True},
                        ],
                        "footer": {"text": "Yahoo!仕入れモード | 種→派生発掘"},
                    }],
                }
                requests.post(webhook, json=payload, timeout=10)
    except Exception as e:
        print(f"[SOURCING] 通知エラー: {e}", flush=True)
