import logging
from apscheduler.schedulers.background import BackgroundScheduler
from database import supabase
from research.keepa_deals import get_keepa_deals, parse_deal
from research.profit import calculate_profit
from research.notify import send_line_notification

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "min_profit_rate": 15,
    "min_profit_amount": 500,
    "min_drop_rate": 20,
    "max_rank": 100000,
    "amazon_fee_rate": 15.4,
    "notify_enabled": False,
    "line_user_id": None,
}


def run_harvest_for_all_users():
    try:
        response = supabase.table("harvest_settings").select("*").execute()
        users = response.data or []
        if not users:
            # 設定がないユーザーには管理者の設定でデフォルト実行
            print("[HARVEST] 設定ユーザーなし、スキップ", flush=True)
            return

        # Keepa Deals APIは全ユーザー共通で1回だけ呼ぶ（トークン節約）
        # 最も広い条件（最小の値下がり率）で取得し、ユーザーごとにフィルタ
        min_drop = min(
            s.get("min_drop_rate", 20) for s in users
        )
        max_rank = max(
            s.get("max_rank", 100000) for s in users
        )

        raw_deals = get_keepa_deals(
            min_drop_percent=min_drop,
            max_rank=max_rank,
            date_range=2,
        )

        parsed = [parse_deal(d) for d in raw_deals]
        parsed = [p for p in parsed if p is not None]
        print(f"[HARVEST] 有効ディール: {len(parsed)}件", flush=True)

        for setting in users:
            try:
                run_harvest_for_user(setting, parsed)
            except Exception as e:
                logger.error(f"User {setting.get('user_id')} error: {e}")

    except Exception as e:
        logger.error(f"Scheduler error: {e}")


def run_harvest_for_user(setting: dict, parsed_deals: list = None):
    user_id = setting["user_id"]
    min_profit_rate = setting.get("min_profit_rate", DEFAULT_SETTINGS["min_profit_rate"])
    min_profit_amount = setting.get("min_profit_amount", DEFAULT_SETTINGS["min_profit_amount"])
    min_drop_rate = setting.get("min_drop_rate", DEFAULT_SETTINGS["min_drop_rate"])
    max_rank = setting.get("max_rank", DEFAULT_SETTINGS["max_rank"])
    amazon_fee_rate = setting.get("amazon_fee_rate", DEFAULT_SETTINGS["amazon_fee_rate"])
    line_user_id = setting.get("line_user_id")
    notify_enabled = setting.get("notify_enabled", False)

    print(f"[HARVEST] ユーザー処理開始 user_id={user_id}", flush=True)

    # 直接呼ばれた場合はAPIを叩く
    if parsed_deals is None:
        raw_deals = get_keepa_deals(
            min_drop_percent=min_drop_rate,
            max_rank=max_rank,
            date_range=2,
        )
        parsed_deals = [parse_deal(d) for d in raw_deals]
        parsed_deals = [p for p in parsed_deals if p is not None]

    profitable = []

    for deal in parsed_deals:
        # ユーザー条件でフィルタ
        if deal["price_drop_rate"] < min_drop_rate:
            continue
        if max_rank > 0 and deal["amazon_rank"] > max_rank:
            continue

        profit_result = calculate_profit(
            buy_price=deal["current_price"],
            sell_price=deal["regular_price"],
            amazon_fee_rate=amazon_fee_rate,
        )

        print(
            f"[HARVEST]   {deal['product_name'][:25]} "
            f"仕入:{deal['current_price']}円 転売:{deal['regular_price']}円 "
            f"利益率:{profit_result['profit_rate']}%",
            flush=True,
        )

        if (
            profit_result["profit_rate"] >= min_profit_rate
            and profit_result["profit_amount"] >= min_profit_amount
        ):
            record = {
                "user_id": user_id,
                "amazon_asin": deal["asin"],
                "product_name": deal["product_name"],
                "current_price": deal["current_price"],
                "regular_price": deal["regular_price"],
                "price_drop_rate": deal["price_drop_rate"],
                "amazon_rank": deal["amazon_rank"],
                "profit_amount": profit_result["profit_amount"],
                "profit_rate": profit_result["profit_rate"],
                "amazon_fee_rate": amazon_fee_rate,
            }
            profitable.append(record)

            # DBに保存（重複チェック: 同一ASIN×同日は1件まで）
            try:
                existing = (
                    supabase.table("harvest_results")
                    .select("id")
                    .eq("user_id", user_id)
                    .eq("amazon_asin", deal["asin"])
                    .gte("found_at", "now() - interval '12 hours'")
                    .execute()
                )
                if not existing.data:
                    supabase.table("harvest_results").insert(record).execute()
                    print(f"[HARVEST]   → 保存: {deal['asin']}", flush=True)
                else:
                    print(f"[HARVEST]   → 重複スキップ: {deal['asin']}", flush=True)
            except Exception as e:
                print(f"[HARVEST]   → DB保存エラー: {e}", flush=True)

    print(f"[HARVEST] 完了: 候補{len(profitable)}件 user={user_id}", flush=True)

    if profitable and notify_enabled and line_user_id:
        send_line_notification(line_user_id, profitable)


def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_harvest_for_all_users, "interval", minutes=30)
    scheduler.start()
    logger.info("Harvest Scheduler started - 30分ごとに実行")
