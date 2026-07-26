import logging
from apscheduler.schedulers.background import BackgroundScheduler
from database import supabase
from research.keepa_deals import get_keepa_deals, parse_deal
from research.profit import calculate_profit
from research.notify import notify_all
from research.seller_health import check_seller_health

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "min_profit_rate": 5,
    "min_profit_amount": 500,
    "min_drop_rate": 23,
    "min_rank": 1,
    "max_rank": 100000,
    "amazon_fee_rate": 18.0,
    "notify_enabled": False,
    "line_user_id": None,
    "discord_webhook_url": None,
}


def _fetch_parsed_deals(min_drop: float, max_rank: int) -> list:
    """Keepa Deals APIから取得してparse済みリストを返す"""
    raw_deals = []
    for page in range(3):
        page_deals = get_keepa_deals(
            min_drop_percent=min_drop,
            max_rank=max_rank,
            date_range=3,
            page=page,
        )
        if not page_deals:
            break
        raw_deals.extend(page_deals)
    print(f"[HARVEST] Keepa取得合計: {len(raw_deals)}件", flush=True)

    parsed = [parse_deal(d) for d in raw_deals]
    parsed = [p for p in parsed if p is not None]
    print(f"[HARVEST] 有効ディール: {len(parsed)}件", flush=True)
    return parsed


def run_harvest_for_all_users():
    """全ユーザー向けにKeepa Dealsをスキャンして刈り取り候補を保存・通知する"""
    try:
        response = supabase.table("harvest_settings").select("*").execute()
        users = response.data or []
        if not users:
            print("[HARVEST] 設定ユーザーなし、スキップ", flush=True)
            return

        min_drop = min(s.get("min_drop_rate", DEFAULT_SETTINGS["min_drop_rate"]) for s in users)
        max_rank = max(s.get("max_rank", DEFAULT_SETTINGS["max_rank"]) for s in users)

        parsed = _fetch_parsed_deals(min_drop, max_rank)

        # セラー健全性チェックは run_harvest_for_user 内で実施
        # （価格・利益フィルター後に絞られた少数のASINだけチェックしてトークン節約）
        health_cache: dict[str, dict] = {}

        for setting in users:
            try:
                run_harvest_for_user(setting, parsed, health_cache)
            except Exception as e:
                logger.error(f"User {setting.get('user_id')} error: {e}")

    except Exception as e:
        logger.error(f"Scheduler error: {e}")


def run_harvest_for_user(setting: dict, parsed_deals: list = None, health_cache: dict = None):
    user_id = setting["user_id"]
    min_profit_rate  = setting.get("min_profit_rate",  DEFAULT_SETTINGS["min_profit_rate"])
    min_profit_amount = setting.get("min_profit_amount", DEFAULT_SETTINGS["min_profit_amount"])
    min_drop_rate    = setting.get("min_drop_rate",    DEFAULT_SETTINGS["min_drop_rate"])
    min_rank         = setting.get("min_rank",         DEFAULT_SETTINGS["min_rank"])
    max_rank         = setting.get("max_rank",         DEFAULT_SETTINGS["max_rank"])
    amazon_fee_rate  = setting.get("amazon_fee_rate",  DEFAULT_SETTINGS["amazon_fee_rate"])

    print(f"[HARVEST] ユーザー処理開始 user_id={user_id}", flush=True)

    if parsed_deals is None:
        parsed_deals = _fetch_parsed_deals(min_drop_rate, max_rank)

    if health_cache is None:
        health_cache = {}

    profitable = []

    for deal in parsed_deals:
        # ① 値下がり率フィルター
        if deal["price_drop_rate"] < min_drop_rate:
            continue

        # ② ランキング範囲フィルター（rank=0は除外）
        rank = deal["amazon_rank"]
        if rank == 0:
            continue
        if min_rank > 1 and rank < min_rank:
            continue
        if max_rank > 0 and rank > max_rank:
            continue

        # ③ 利益計算・利益フィルター
        profit_result = calculate_profit(
            buy_price=deal["current_price"],
            sell_price=deal["regular_price"],
            amazon_fee_rate=amazon_fee_rate,
        )
        if (
            profit_result["profit_rate"] < min_profit_rate
            or profit_result["profit_amount"] < min_profit_amount
        ):
            continue

        # ④ セラー健全性チェック（①〜③通過後のみ実行してトークン節約）
        asin = deal["asin"]
        if asin not in health_cache:
            health_cache[asin] = check_seller_health(asin, deal.get("root_category", 0))
        if not health_cache[asin].get("healthy", True):
            reason = health_cache[asin].get("reason", "")
            print(f"[HARVEST]   → セラーNG: {asin} ({reason})", flush=True)
            continue

        record = {
            "user_id": user_id,
            "amazon_asin": asin,
            "product_name": deal["product_name"],
            "current_price": deal["current_price"],
            "regular_price": deal["regular_price"],
            "price_drop_rate": deal["price_drop_rate"],
            "amazon_rank": deal["amazon_rank"],
            "profit_amount": profit_result["profit_amount"],
            "profit_rate": profit_result["profit_rate"],
            "amazon_fee_rate": amazon_fee_rate,
        }

        # 直近12時間で同一ASINが既に保存済みなら重複スキップ
        try:
            from datetime import datetime, timezone, timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
            existing = (
                supabase.table("harvest_results")
                .select("id")
                .eq("user_id", user_id)
                .eq("amazon_asin", asin)
                .gte("found_at", cutoff)
                .execute()
            )
            if not existing.data:
                supabase.table("harvest_results").insert(record).execute()
                profitable.append(record)
                print(f"[HARVEST]   → 新規保存: {asin} 利益率:{profit_result['profit_rate']}%", flush=True)
            else:
                print(f"[HARVEST]   → 重複スキップ: {asin}", flush=True)
        except Exception as e:
            print(f"[HARVEST]   → DB保存エラー: {e}", flush=True)

    print(f"[HARVEST] 完了: 新規候補{len(profitable)}件 user={user_id}", flush=True)

    if profitable:
        notify_all(setting, profitable)


def start_scheduler():
    scheduler = BackgroundScheduler()
    # メイン監視はKeepaトラッカー(Webhookプッシュ)。
    # Dealsスキャン(新規発掘)の間隔はプランの補充レートから自動決定する。
    from research.keepa_budget import deals_scan_interval_hours
    try:
        hours = deals_scan_interval_hours()
    except Exception:
        hours = 3
    scheduler.add_job(run_harvest_for_all_users, "interval", hours=hours)

    # Yahoo!仕入れモード: 実質価格（ポイント込み）の再チェックを毎日実行。
    # Yahoo! APIは無料なのでKeepaトークンを一切消費しない。
    # レート制限（1分30件）で中断しても自動で待って再開する
    def rescan_yahoo():
        try:
            from research.sourcing import rescan_until_complete
            rescan_until_complete(scenario="auto")
        except Exception as e:
            logger.error(f"Yahoo rescan error: {e}")
    scheduler.add_job(rescan_yahoo, "interval", hours=24)

    # デプロイ直後に1度だけ「日曜」条件で全件更新する。
    # 5のつく日と日曜が重なるのは年に数回しかないため、毎週来る日曜を基準にする。
    # 直前のレート制限から回復するため20分待ってから開始する
    def initial_rescan():
        try:
            from research.sourcing import rescan_until_complete
            logger.info("初回の全件再チェックを開始（条件: 日曜）")
            rescan_until_complete(scenario="sunday")
        except Exception as e:
            logger.error(f"Initial rescan error: {e}")
    import threading
    threading.Timer(1200, initial_rescan).start()

    # 再起動でタイマーが消えても取りこぼさないよう、3時間ごとに
    # 「更新が12時間以上前の候補が残っていないか」を見て残りを埋める
    def fill_stale():
        try:
            from datetime import datetime, timezone, timedelta
            from database import supabase
            from research.sourcing import rescan_until_complete
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
            stale = (
                supabase.table("sourcing_candidates")
                .select("id")
                .lt("updated_at", cutoff)
                .limit(1)
                .execute()
            )
            if stale.data:
                logger.info("更新が古い候補を検出。全件再チェックを実行")
                rescan_until_complete(scenario="sunday", max_rounds=4)
        except Exception as e:
            logger.error(f"Fill stale error: {e}")
    scheduler.add_job(fill_stale, "interval", hours=3)

    # Render Freeはバックグラウンドスレッドが再起動で落ちるため、
    # 中断された発掘ジョブを定期的に拾って再開する（評価済みはスキップされる）
    def resume_sourcing():
        try:
            from research.sourcing import resume_stalled_seeds
            resume_stalled_seeds()
        except Exception as e:
            logger.error(f"Sourcing resume error: {e}")
    scheduler.add_job(resume_sourcing, "interval", minutes=20)

    scheduler.start()
    logger.info(f"Harvest Scheduler started - Dealsスキャン{hours}時間ごと / Yahoo!再スキャン24時間ごと")
