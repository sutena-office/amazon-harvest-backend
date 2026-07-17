from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from routers import users, settings, deals, pool
from scheduler import start_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield


app = FastAPI(title="Amazon Harvest App", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(users.router, prefix="/api/users", tags=["users"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])
app.include_router(deals.router, prefix="/api/deals", tags=["deals"])
app.include_router(pool.router, prefix="/api/pool", tags=["pool"])


@app.get("/")
def root():
    return {"status": "ok", "app": "Amazon Harvest"}


@app.get("/health")
def health():
    """keep-alive用。cron-job.org等から10分ごとにpingすることで
    Render Freeのスリープを防ぎ、スケジューラーとWebhook受信を常時稼働させる。
    DBに軽く触れることで、Supabase Free側の無操作による自動一時停止も防ぐ
    （HTTPを叩くだけではRenderは起きてもSupabaseへのアクセスにはならないため）。"""
    db_status = "unknown"
    try:
        from database import supabase
        supabase.table("harvest_settings").select("user_id").limit(1).execute()
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {e}"
    return {"status": "ok", "db": db_status}


# ────────────────────────────────────────────────────────
# Keepa Tracking Webhook（監視エンジンの心臓部）
# watch_listのトラッカーが目標価格を割った瞬間、Keepaがここへプッシュしてくる
# ────────────────────────────────────────────────────────
@app.post("/webhook/keepa")
async def keepa_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    print(f"[KEEPA_WEBHOOK] 受信: {str(body)[:500]}", flush=True)

    asin = body.get("asin", "")
    domain = body.get("domain", 0)
    if not asin or (domain and domain != 5):
        return {"status": "skipped"}

    try:
        from database import supabase
        from research.keepa_deals import get_keepa_data_by_asin
        from research.profit import calculate_profit
        from research.notify import notify_all
        from datetime import datetime, timezone, timedelta

        # 商品詳細はASINにつき1回だけ取得（トークン節約）
        deal_info = get_keepa_data_by_asin(asin)

        users_res = (
            supabase.table("harvest_settings")
            .select("*")
            .eq("notify_enabled", True)
            .execute()
        )

        for setting in users_res.data or []:
            user_id = setting["user_id"]
            amazon_fee_rate = setting.get("amazon_fee_rate", 18.0)
            min_profit_rate = setting.get("min_profit_rate", 5)
            min_profit_amount = setting.get("min_profit_amount", 500)

            # watch_listにある商品なら審査済みの中央値を「販売価格」として使う（最も正確）
            watch_res = (
                supabase.table("watch_list")
                .select("median_price_90d, seller_count, amazon_in_stock, product_name")
                .eq("user_id", user_id)
                .eq("asin", asin)
                .execute()
            )
            watch = watch_res.data[0] if watch_res.data else None

            if watch and deal_info:
                sell_price = watch["median_price_90d"]
                buy_price = deal_info["current_price"]
                product_name = watch.get("product_name") or deal_info.get("product_name", "")
                rank = deal_info.get("amazon_rank", 0)
            elif deal_info:
                sell_price = deal_info["regular_price"]
                buy_price = deal_info["current_price"]
                product_name = deal_info.get("product_name", "")
                rank = deal_info.get("amazon_rank", 0)
            else:
                continue

            if not buy_price or not sell_price or buy_price >= sell_price:
                continue

            drop_rate = round((sell_price - buy_price) / sell_price * 100, 1)
            profit_result = calculate_profit(
                buy_price=buy_price,
                sell_price=sell_price,
                amazon_fee_rate=amazon_fee_rate,
            )

            if (
                profit_result["profit_rate"] < min_profit_rate
                or profit_result["profit_amount"] < min_profit_amount
            ):
                continue

            record = {
                "user_id": user_id,
                "amazon_asin": asin,
                "product_name": product_name,
                "current_price": buy_price,
                "regular_price": sell_price,
                "price_drop_rate": drop_rate,
                "amazon_rank": rank,
                "profit_amount": profit_result["profit_amount"],
                "profit_rate": profit_result["profit_rate"],
                "amazon_fee_rate": amazon_fee_rate,
            }

            cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
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
                notify_all(setting, [record])
                print(f"[KEEPA_WEBHOOK] 即時通知: {asin} 利益率{profit_result['profit_rate']}%", flush=True)

    except Exception as e:
        print(f"[KEEPA_WEBHOOK] 処理エラー: {e}", flush=True)

    return {"status": "ok"}
