from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from routers import users, settings, deals
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


@app.get("/")
def root():
    return {"status": "ok", "app": "Amazon Harvest"}


# ────────────────────────────────────────────────────────
# Keepa Notificationウェブフック
# KeepaアカウントのWebhook URLにこのエンドポイントを設定すると
# Keepaが価格変動を検知した瞬間にリアルタイム通知を受信できる
# ────────────────────────────────────────────────────────
@app.post("/webhook/keepa")
async def keepa_webhook(request: Request):
    """
    KeepaからのPrice Change通知を受信して即時処理する。
    Keepaアカウントページの「Notification webhook endpoint」に
    このURLを設定してください。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    print(f"[KEEPA_WEBHOOK] 受信: {body}", flush=True)

    # Keepaのwebhookペイロードを解析
    asin = body.get("asin", "")
    domain = body.get("domain", 0)
    current_price = body.get("currentPrice", 0)

    if not asin or domain != 5:  # domain=5 はamazon.co.jp
        return {"status": "skipped"}

    # 全ユーザーの設定を取得してリアルタイム処理
    try:
        from database import supabase
        from research.keepa_deals import get_keepa_data_by_asin
        from research.profit import calculate_profit
        from research.notify import notify_all

        users_res = supabase.table("harvest_settings").select("*").eq("notify_enabled", True).execute()

        for setting in (users_res.data or []):
            amazon_fee_rate = setting.get("amazon_fee_rate", 15.4)
            min_profit_rate = setting.get("min_profit_rate", 15)
            min_profit_amount = setting.get("min_profit_amount", 500)
            min_drop_rate = setting.get("min_drop_rate", 20)

            # Keepa APIでASINの詳細を取得
            deal_info = get_keepa_data_by_asin(asin)
            if not deal_info:
                continue

            drop_rate = deal_info.get("price_drop_rate", 0)
            if drop_rate < min_drop_rate:
                continue

            profit_result = calculate_profit(
                buy_price=deal_info["current_price"],
                sell_price=deal_info["regular_price"],
                amazon_fee_rate=amazon_fee_rate,
            )

            if (profit_result["profit_rate"] >= min_profit_rate
                    and profit_result["profit_amount"] >= min_profit_amount):

                record = {
                    "user_id": setting["user_id"],
                    "amazon_asin": asin,
                    "product_name": deal_info.get("product_name", ""),
                    "current_price": deal_info["current_price"],
                    "regular_price": deal_info["regular_price"],
                    "price_drop_rate": drop_rate,
                    "amazon_rank": deal_info.get("amazon_rank", 0),
                    "profit_amount": profit_result["profit_amount"],
                    "profit_rate": profit_result["profit_rate"],
                    "amazon_fee_rate": amazon_fee_rate,
                }

                # 重複チェック
                existing = (
                    supabase.table("harvest_results")
                    .select("id")
                    .eq("user_id", setting["user_id"])
                    .eq("amazon_asin", asin)
                    .gte("found_at", "now() - interval '1 hours'")
                    .execute()
                )
                if not existing.data:
                    supabase.table("harvest_results").insert(record).execute()
                    notify_all(setting, [record])
                    print(f"[KEEPA_WEBHOOK] 通知送信: {asin}", flush=True)

    except Exception as e:
        print(f"[KEEPA_WEBHOOK] 処理エラー: {e}", flush=True)

    return {"status": "ok"}
