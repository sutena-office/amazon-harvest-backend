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

# 実務者の判定基準（月5〜10万を安定して稼ぐ商品の条件）
CRIT_SALES_MIN = 200     # 月販200個以上
CRIT_SALES_GOOD = 300    # 300個以上なら理想的
CRIT_RANK_IDEAL = 5000   # ランク5,000位以内なら早く売れる
CRIT_RANK_MAX = 10000    # 1万位が実質的な上限
# 出品者が3〜5人しかいない商品は「卸契約者しか出品できない」危険サイン。
# 新規は真贋・卸証明を求められて出品できない可能性が高いため10人以上を必須とする
CRIT_SELLERS_MIN = 10
CRIT_SELLERS_GOOD = 12


def grade_candidate(monthly_sales: int, rank: int, sellers: int) -> dict:
    """実務基準で商品をランク付けする。
    S=全条件を理想水準で満たす / A=条件クリア / B=条件は満たすが弱い / C=不適合"""
    reasons = []
    fatal = False

    if sellers < CRIT_SELLERS_MIN:
        reasons.append(f"出品者{sellers}人（卸契約者限定の疑い・要{CRIT_SELLERS_MIN}人以上）")
        fatal = True
    if rank <= 0 or rank > CRIT_RANK_MAX:
        reasons.append(f"ランク{rank:,}位（上限{CRIT_RANK_MAX:,}位）")
        fatal = True
    if monthly_sales < CRIT_SALES_MIN:
        reasons.append(f"月販{monthly_sales}個（要{CRIT_SALES_MIN}個以上）")
        fatal = True

    if fatal:
        return {"grade": "C", "note": " / ".join(reasons)}

    ideal = (
        monthly_sales >= CRIT_SALES_GOOD
        and rank <= CRIT_RANK_IDEAL
        and sellers >= CRIT_SELLERS_GOOD
    )
    if ideal:
        return {"grade": "S", "note": "全条件が理想水準（月販300+・5千位以内・出品者12人+）"}
    if rank <= CRIT_RANK_IDEAL:
        return {"grade": "A", "note": "条件クリア（5,000位以内）"}
    return {"grade": "B", "note": f"条件は満たすがランク{rank:,}位とやや弱い"}


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


def _query_finder(selection: dict) -> list:
    """Product Finderを1回叩く。失敗時は空リスト"""
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
        return (res.json() or {}).get("asinList") or []
    except Exception as e:
        print(f"[SOURCING] Product Finder例外: {e}", flush=True)
        return []


def discover_similar_asins(traits: dict) -> list:
    """Product Finderで種に似た商品を探す。

    重要: 判定基準（出品者10人以上・ランク1万位以内・月販200個以上）を
    ここで先に適用する。1件ずつの評価は1商品=1トークンかかるため、
    落ちると分かっている商品を評価対象に入れないことがトークン節約の要。
    """
    price = traits["reference_price"]
    base = {
        "rootCategory": [int(traits["root_category"])],
        "current_NEW_gte": int(price * 0.4),
        "current_NEW_lte": int(price * 2.5),
        "current_COUNT_NEW_gte": CRIT_SELLERS_MIN,   # 出品者10人以上
        "current_SALES_gte": 1,
        "current_SALES_lte": CRIT_RANK_MAX,          # ランク1万位以内
        "monthlySold_gte": CRIT_SALES_MIN,           # 月販200個以上
        "perPage": MAX_CANDIDATES,
        "page": 0,
    }

    # 段階的に条件を緩める。上ほど質が高い
    steps = [
        ("基準厳守・同ブランド", {**base, "brand": [traits["brand"]]} if traits.get("brand") else base),
        ("基準厳守・全ブランド", base),
        # monthlySoldは未設定の商品が多いため、0件なら外して評価側で判定する
        ("月販条件を外す・同ブランド",
         {**{k: v for k, v in base.items() if k != "monthlySold_gte"},
          **({"brand": [traits["brand"]]} if traits.get("brand") else {})}),
        ("月販条件を外す・全ブランド",
         {k: v for k, v in base.items() if k != "monthlySold_gte"}),
    ]

    asins: list = []
    for label, sel in steps:
        found = _query_finder(sel)
        asins = list(dict.fromkeys(asins + found))[:MAX_CANDIDATES]
        print(f"[SOURCING] 候補抽出[{label}] +{len(found)}件 → 累計{len(asins)}件", flush=True)
        if len(asins) >= 40:
            break

    print(f"[SOURCING] 判定基準で事前絞り込み済み {len(asins)}件を評価対象とする", flush=True)
    return asins


def _profit_calc(amazon_price: int, yahoo_price: int, store_point: int = 0,
                 coupon: int = None, extra_rate: float = 0.0, dt=None) -> dict:
    """実務の計算式で利益を算出する。

    実質仕入れ値 =（表示価格 − クーポン）−（支払額 ÷ 1.1 × 総還元率）
    利益         = Amazon売価 ×(1−経費率) − 実質仕入れ値
    """
    from research.yahoo_points import effective_cost
    ec = effective_cost(yahoo_price, store_point=store_point,
                        coupon=coupon, dt=dt, extra_rate=extra_rate)
    sell_net = int(amazon_price * (1 - FEE_RATE))
    profit = sell_net - ec["effective"]
    rate = round(profit / amazon_price * 100, 1) if amazon_price else 0
    return {
        "profit_amount": profit,
        "profit_rate": rate,
        "effective": ec["effective"],
        "total_point": ec["total_point"],
        "total_rate": ec["total_rate"],
        "coupon": ec["coupon"],
        "payment": ec["payment"],
    }


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

    # 月販数: monthlySoldはAmazonの「過去1か月に○個購入」の実データ（推定ではない）。
    # 未設定の商品が多いため、その場合のみランク下落回数で代用する（過小評価になる）
    stats = p.get("stats") or {}
    monthly_sold = int(p.get("monthlySold") or 0)
    est_monthly_sales = monthly_sold or int(stats.get("salesRankDrops30") or 0)
    sales_is_actual = monthly_sold > 0
    seller_count = _stat(p, "current", 11) or _stat(p, "avg90", 11)

    yahoo = search_best_by_jan(ean_list[0])
    if not yahoo:
        return None

    pr = _profit_calc(amazon_price, yahoo["price"], store_point=yahoo.get("store_point", 0))
    # 実現可能な月間利益 = 利益/個 × min(月の仕入れ上限, カートで取れる見込み個数)
    # 「市場全体の月販 × 利益」ではなく、自分が実際に回せる数で見積もる
    student_monthly = _student_share(pr["profit_amount"], est_monthly_sales, seller_count)
    expected_monthly = student_monthly

    g = grade_candidate(est_monthly_sales, rank, seller_count)

    return {
        "asin": asin,
        "jan": ean_list[0],
        "product_name": title,
        "amazon_price": amazon_price,
        "amazon_rank": rank,
        "est_monthly_sales": est_monthly_sales,
        "sales_is_actual": sales_is_actual,
        "seller_count": seller_count,
        "grade": g["grade"],
        "grade_note": g["note"],
        "yahoo_price": yahoo["price"],
        "yahoo_point": pr["total_point"],
        "yahoo_effective": pr["effective"],
        "yahoo_url": yahoo["url"],
        "yahoo_store": yahoo["store"],
        "profit_amount": pr["profit_amount"],
        "profit_rate": pr["profit_rate"],
        "expected_monthly_profit": expected_monthly,
        "student_monthly_profit": student_monthly,
    }


# 1商品あたり月に仕入れられる個数の上限。
# Yahoo!ストアの購入制限（「お一人さま1点限り」）とポイント付与上限があるため、
# 仕入れ日（5のつく日・日曜）ごとに1個ずつ買う運用を想定する。
MAX_UNITS_PER_MONTH = int(os.getenv("SOURCING_MAX_UNITS_PER_MONTH", "4"))


def _sellable_units(monthly_sales: int, seller_count: int) -> float:
    """自分が新規参入した場合に月に売れる見込み個数（カート取得の簡易モデル）"""
    if monthly_sales <= 0:
        return 0.0
    return monthly_sales / max(seller_count + 1, 1)


def _student_share(profit: int, monthly_sales: int, seller_count: int) -> int:
    """1人が現実に得られる月間利益。

    「売れる数」と「買える数」の小さい方で決まる。
    高額商品はYahoo!側の購入制限が効くため、実務では買える数が上限になる。
    """
    if profit <= 0 or monthly_sales <= 0:
        return 0
    units = min(float(MAX_UNITS_PER_MONTH), _sellable_units(monthly_sales, seller_count))
    return int(profit * units)


def resume_stalled_seeds():
    """中断された発掘ジョブを再開する。
    Render Freeはバックグラウンドスレッドが再起動で落ちるため、
    起動時とスケジューラから定期的に呼んで取りこぼしを防ぐ。"""
    try:
        res = (
            supabase.table("sourcing_seeds")
            .select("*")
            .eq("status", "running")
            .execute()
        )
        for seed in res.data or []:
            # 進捗が全件に達していれば完了扱いにする
            if seed.get("total") and seed.get("checked", 0) >= seed["total"]:
                supabase.table("sourcing_seeds").update(
                    {"status": "done",
                     "finished_at": datetime.now(timezone.utc).isoformat()}
                ).eq("id", seed["id"]).execute()
                continue

            import threading
            traits = {
                "asin": seed["asin"],
                "title": seed.get("title", ""),
                "brand": seed.get("brand", ""),
                "root_category": seed.get("root_category") or 0,
                "reference_price": seed.get("reference_price") or 0,
            }
            print(f"[SOURCING] 中断ジョブを再開: {seed['asin']} "
                  f"({seed.get('checked',0)}/{seed.get('total',0)})", flush=True)
            threading.Thread(
                target=run_sourcing_job,
                args=(seed["id"], seed["user_id"], traits),
                daemon=True,
            ).start()
    except Exception as e:
        print(f"[SOURCING] 再開処理エラー: {e}", flush=True)


def run_sourcing_job(seed_id: str, user_id: str, traits: dict):
    """種→派生の発掘バッチ（バックグラウンドスレッドで実行）。
    評価済みASINはスキップするため、中断→再実行で続きから再開できる。"""
    asins = discover_similar_asins(traits)
    if not asins:
        supabase.table("sourcing_seeds").update(
            {"status": "done", "total": 0, "checked": 0, "hits": 0}
        ).eq("id", seed_id).execute()
        return

    # このseedで評価済みのASINは飛ばす（中断→再開のため）
    done_res = (
        supabase.table("sourcing_candidates")
        .select("asin, profit_amount")
        .eq("user_id", user_id)
        .eq("seed_id", seed_id)
        .execute()
    )
    done_rows = done_res.data or []
    done_asins = {r["asin"] for r in done_rows}
    remaining = [a for a in asins if a not in done_asins]

    checked = len(asins) - len(remaining)
    hits = sum(1 for r in done_rows if (r.get("profit_amount") or 0) > 0)

    # 残件数でペースを算出する（手持ちトークンも考慮）
    from research.keepa_budget import screening_pace_seconds
    pace = screening_pace_seconds(len(remaining)) if remaining else 3

    supabase.table("sourcing_seeds").update(
        {"total": len(asins), "checked": checked, "hits": hits}
    ).eq("id", seed_id).execute()
    eta_h = round(len(remaining) * pace / 3600, 1)
    print(f"[SOURCING] 発掘開始 seed={seed_id} 候補={len(asins)}件 "
          f"（済{checked}件はスキップ・残{len(remaining)}件）"
          f"ペース={pace}秒/件 完了まで約{eta_h}時間", flush=True)

    for asin in remaining:
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

        # 進捗は毎件反映する（10件ごとだと画面上は止まって見えるため）
        try:
            supabase.table("sourcing_seeds").update(
                {"checked": checked, "hits": hits}
            ).eq("id", seed_id).execute()
        except Exception:
            pass
        if checked % 10 == 0 or checked == len(asins):
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

    # boost_percent は「今日の還元に対する追加上乗せ」（超PayPay祭等）として扱う。
    # 未指定なら今日の日付から判定した還元率がそのまま使われる。
    extra_rate = boost_percent or 0.0
    from research.yahoo_points import campaign_for_date
    print(f"[SOURCING] 本日の還元: {campaign_for_date()['summary']} (+追加{extra_rate}%)", flush=True)

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
            row["amazon_price"], yahoo["price"],
            store_point=yahoo.get("store_point", 0),
            extra_rate=extra_rate,
        )
        est_sales = int(row.get("est_monthly_sales") or 0)
        expected_monthly = pr["profit_amount"] * est_sales if pr["profit_amount"] > 0 else 0
        student_monthly = _student_share(
            pr["profit_amount"], est_sales, int(row.get("seller_count") or 0)
        )

        supabase.table("sourcing_candidates").update(
            {
                "yahoo_price": yahoo["price"],
                "yahoo_point": pr["total_point"],
                "yahoo_effective": pr["effective"],
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
                "yahoo_effective": pr["effective"],
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
