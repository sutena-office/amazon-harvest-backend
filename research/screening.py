"""
監視プールの審査バッチ。
Product Finderで取得したASINを1件ずつ審査し、合格分をwatch_listに登録する。
1 ASIN ≈ 1トークン。5トークン/分プランに合わせて約13秒/件でペーシングし、
数千件を夜間に消化する設計。
"""
import os
import time
import statistics
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from database import get_client

load_dotenv()
KEEPA_API_KEY = os.getenv("KEEPA_API_KEY")

# APIリクエスト処理用の共有クライアントとは別に、この長時間バックグラウンド
# 処理専用のクライアントを持つ（互いの負荷が干渉しないようにするため）
supabase = get_client()

KEEPA_TIME_OFFSET = 21564000  # Keepa分 = unix分 - 21564000
PACE_SECONDS = 13             # 5トークン/分プランで枯渇しないペース
TARGET_RATIO = 0.77           # 仕入れ目標価格 = 中央値 × 0.77（経費18%+利益5%）

# 目利きできない商材（輸入品・音楽系）を除外するためのキーワード/カテゴリ
IMPORT_KEYWORDS = [
    "輸入盤", "直輸入", "並行輸入", "海外盤", "北米版", "輸入版",
    "海外正規品", "import", "洋楽", "韓国盤", "台湾盤",
]
MUSIC_CATEGORY_NAMES = {"ミュージック", "デジタルミュージック"}


def _is_import_or_music(title: str, root_category: int) -> bool:
    lower_title = title.lower()
    if any(kw.lower() in lower_title for kw in IMPORT_KEYWORDS):
        return True
    try:
        from research.keepa_pool import get_root_categories
        cats = get_root_categories()
        name = next((c["name"] for c in cats if c["id"] == root_category), "")
        if name in MUSIC_CATEGORY_NAMES:
            return True
    except Exception:
        pass
    return False


def _keepa_minutes_ago(days: int) -> int:
    unix_min = int(datetime.now(timezone.utc).timestamp() / 60)
    return unix_min - KEEPA_TIME_OFFSET - days * 24 * 60


def _median_price(history: list, days: int = 90) -> int:
    """Keepa履歴配列 [time, value, time, value, ...] から直近days日の中央値"""
    if not history or len(history) < 2:
        return 0
    cutoff = _keepa_minutes_ago(days)
    values = []
    last_value = 0
    for i in range(0, len(history) - 1, 2):
        t, v = history[i], history[i + 1]
        if v and v > 0:
            last_value = v
        if t >= cutoff and v and v > 0:
            values.append(v)
    # 期間内に更新がない＝価格が安定して張り付いている場合は直近値を使う
    if not values and last_value > 0:
        values = [last_value]
    return int(statistics.median(values)) if values else 0


def _arr(a: list, i: int) -> int:
    if not a or len(a) <= i:
        return 0
    v = a[i]
    return int(v) if v and v > 0 else 0


def screen_asin(asin: str, criteria: dict) -> dict:
    """1 ASINを審査する（1トークン消費）"""
    url = "https://api.keepa.com/product"
    params = {
        "key": KEEPA_API_KEY,
        "domain": 5,
        "asin": asin,
        "stats": 90,
        "history": 1,
    }
    try:
        res = requests.get(url, params=params, timeout=20)
        if res.status_code == 429:
            return {"ok": False, "retry": True, "reason": "トークン待ち"}
        if res.status_code != 200:
            return {"ok": False, "reason": f"APIエラー{res.status_code}"}

        data = res.json()
        products = data.get("products") or []
        if not products:
            return {"ok": False, "reason": "商品データなし"}

        p = products[0]
        title = (p.get("title") or "").strip()

        if _is_import_or_music(title, p.get("rootCategory") or 0):
            return {"ok": False, "reason": "輸入品/音楽系のため除外"}

        csv = p.get("csv") or []
        stats = p.get("stats") or {}
        avg90 = stats.get("avg90") or []
        current = stats.get("current") or []

        # 90日中央値: 新品最安値(1) → Amazon本体(0) の順で採用
        median = _median_price(csv[1] if len(csv) > 1 else [])
        if not median:
            median = _median_price(csv[0] if len(csv) > 0 else [])
        if not median:
            return {"ok": False, "reason": "価格履歴なし"}

        # 出品者健全性（Amazon込み新品出品者数）
        seller_90 = _arr(avg90, 11)
        seller_now = _arr(current, 11)
        if seller_90 < criteria.get("min_sellers", 3):
            return {"ok": False, "reason": f"出品者不足({seller_90}人)"}
        if seller_now > 0 and seller_now < seller_90 * 0.5:
            return {"ok": False, "reason": f"出品者急減({seller_90}→{seller_now}人)"}

        # ランキング
        rank = _arr(current, 3) or _arr(avg90, 3)
        max_rank = criteria.get("max_rank", 50000)
        if rank == 0 or rank > max_rank:
            return {"ok": False, "reason": f"ランク圏外({rank}位)"}

        # Amazon本体の在庫有無（プラス材料として記録）
        amazon_in_stock = _arr(current, 0) > 0

        return {
            "ok": True,
            "asin": asin,
            "product_name": title,
            "median_price_90d": median,
            "target_price": int(median * TARGET_RATIO),
            "seller_count": seller_90,
            "amazon_in_stock": amazon_in_stock,
            "sales_rank": rank,
            "root_category": p.get("rootCategory") or 0,
            "tokens_left": data.get("tokensLeft", 0),
        }

    except Exception as e:
        return {"ok": False, "reason": f"例外: {e}"}


def run_screening_job(job_id: str, user_id: str, asins: list, criteria: dict):
    """
    審査バッチ本体（バックグラウンドスレッドで実行）。
    Render Freeはバックグラウンドスレッドが予告なく落ちることがあるため、
    既に審査済み(watch_listに存在する)ASINは事前に除外し、
    同じ条件で再度「プール構築」を押すだけで続きから再開できるようにする。
    """
    existing_res = supabase.table("watch_list").select("asin").eq("user_id", user_id).execute()
    already_known = {r["asin"] for r in (existing_res.data or [])}
    remaining = [a for a in asins if a not in already_known]

    approved = len(already_known)  # 既存分を合格数の初期値にする
    screened = len(asins) - len(remaining)  # 既存分は審査済み扱い

    print(
        f"[SCREEN] 審査開始 job={job_id} 対象={len(asins)}件 "
        f"（既存{len(already_known)}件はスキップ、残り{len(remaining)}件を審査）",
        flush=True,
    )

    if screened:
        try:
            supabase.table("pool_jobs").update(
                {"screened": screened, "approved": approved}
            ).eq("id", job_id).execute()
        except Exception:
            pass

    for asin in remaining:
        result = screen_asin(asin, criteria)

        if result.get("retry"):
            print("[SCREEN] トークン枯渇、120秒待機", flush=True)
            time.sleep(120)
            result = screen_asin(asin, criteria)

        screened += 1

        if result.get("ok"):
            try:
                supabase.table("watch_list").upsert(
                    {
                        "user_id": user_id,
                        "asin": result["asin"],
                        "product_name": result["product_name"],
                        "median_price_90d": result["median_price_90d"],
                        "target_price": result["target_price"],
                        "seller_count": result["seller_count"],
                        "amazon_in_stock": result["amazon_in_stock"],
                        "sales_rank": result["sales_rank"],
                        "root_category": result["root_category"],
                        "status": "approved",
                        "screened_at": datetime.now(timezone.utc).isoformat(),
                    },
                    on_conflict="user_id,asin",
                ).execute()
                approved += 1
            except Exception as e:
                print(f"[SCREEN] DB保存エラー {asin}: {e}", flush=True)
        # 不合格理由は件数が多いのでログのみ
        elif screened % 50 == 0:
            print(f"[SCREEN] 例: {asin} NG ({result.get('reason')})", flush=True)

        # 進捗を10件ごとにDBへ反映
        if screened % 10 == 0 or screened == len(asins):
            try:
                supabase.table("pool_jobs").update(
                    {"screened": screened, "approved": approved}
                ).eq("id", job_id).execute()
            except Exception:
                pass
            print(f"[SCREEN] 進捗 {screened}/{len(asins)} 合格{approved}", flush=True)

        time.sleep(PACE_SECONDS)

    # 完了処理
    try:
        supabase.table("pool_jobs").update(
            {
                "status": "done",
                "screened": screened,
                "approved": approved,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", job_id).execute()
    except Exception:
        pass
    print(f"[SCREEN] 審査完了 job={job_id} 合格{approved}/{screened}", flush=True)

    # 合格分にKeepaトラッカーを登録
    try:
        from research.tracking import register_trackers_for_user
        register_trackers_for_user(user_id)
    except Exception as e:
        print(f"[SCREEN] トラッカー登録エラー: {e}", flush=True)
