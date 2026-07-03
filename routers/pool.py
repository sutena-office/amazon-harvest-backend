import threading
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional, List
from database import supabase
from auth import get_current_user
from research.keepa_pool import find_pool_asins, get_root_categories, DEFAULT_CRITERIA

router = APIRouter()


class PoolCriteria(BaseModel):
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    min_sellers: Optional[int] = None
    max_rank: Optional[int] = None
    categories: Optional[List[int]] = None


class CsvImport(BaseModel):
    asins: List[str]


@router.get("/categories")
async def categories(current_user=Depends(get_current_user)):
    """日本Amazonのルートカテゴリ一覧（プール条件のカテゴリ選択用）"""
    return get_root_categories()


@router.post("/preview")
async def preview(criteria: PoolCriteria, current_user=Depends(get_current_user)):
    """条件に合う商品件数をプレビュー（Product Finder 1回分のトークンのみ消費）"""
    result = find_pool_asins(criteria.model_dump())
    return {
        "total": result.get("total", 0),
        "fetched": len(result.get("asins", [])),
        "tokens_left": result.get("tokens_left", 0),
        "error": result.get("error"),
    }


@router.post("/build")
async def build(criteria: PoolCriteria, current_user=Depends(get_current_user)):
    """プール構築を開始：ASIN取得→夜間審査バッチをバックグラウンドで起動"""
    try:
        # 実行中ジョブがあれば拒否
        running = (
            supabase.table("pool_jobs")
            .select("id")
            .eq("user_id", current_user.id)
            .eq("status", "running")
            .execute()
        )
        if running.data:
            return {"started": False, "message": "審査ジョブが既に実行中です"}

        result = find_pool_asins(criteria.model_dump())
        asins = result.get("asins", [])
        if not asins:
            return {"started": False, "message": f"該当商品なし: {result.get('error', '')}"}

        job = (
            supabase.table("pool_jobs")
            .insert({"user_id": current_user.id, "status": "running", "total": len(asins)})
            .execute()
        )
        job_id = job.data[0]["id"]
    except Exception as e:
        print(f"[POOL] 構築開始エラー: {e}", flush=True)
        return {"started": False, "message": f"エラー: {str(e)[:200]}"}

    merged = {**DEFAULT_CRITERIA, **{k: v for k, v in criteria.model_dump().items() if v is not None}}

    from research.screening import run_screening_job
    thread = threading.Thread(
        target=run_screening_job,
        args=(job_id, current_user.id, asins, merged),
        daemon=True,
    )
    thread.start()

    hours = round(len(asins) * 13 / 3600, 1)
    return {
        "started": True,
        "job_id": job_id,
        "total": len(asins),
        "message": f"{len(asins)}件の審査を開始しました（完了まで約{hours}時間）",
    }


@router.post("/import-csv")
async def import_csv(payload: CsvImport, current_user=Depends(get_current_user)):
    """CSV由来のASINリストを直接審査にかける（手動インポート用）"""
    asins = [a.strip().upper() for a in payload.asins if a.strip()]
    asins = list(dict.fromkeys(asins))  # 重複除去
    if not asins:
        return {"started": False, "message": "有効なASINがありません"}

    try:
        running = (
            supabase.table("pool_jobs")
            .select("id")
            .eq("user_id", current_user.id)
            .eq("status", "running")
            .execute()
        )
        if running.data:
            return {"started": False, "message": "審査ジョブが既に実行中です"}

        job = (
            supabase.table("pool_jobs")
            .insert({"user_id": current_user.id, "status": "running", "total": len(asins)})
            .execute()
        )
        job_id = job.data[0]["id"]
    except Exception as e:
        print(f"[POOL] CSVインポートエラー: {e}", flush=True)
        return {"started": False, "message": f"エラー: {str(e)[:200]}"}

    from research.screening import run_screening_job
    thread = threading.Thread(
        target=run_screening_job,
        args=(job_id, current_user.id, asins, dict(DEFAULT_CRITERIA)),
        daemon=True,
    )
    thread.start()
    return {"started": True, "job_id": job_id, "total": len(asins)}


@router.post("/register")
async def register(current_user=Depends(get_current_user)):
    """承認済みASINのトラッカー登録を（再）実行する"""
    try:
        from research.tracking import register_trackers_for_user
        result = register_trackers_for_user(current_user.id)
        return {
            "ok": True,
            "registered": result.get("registered", 0),
            "total": result.get("total", 0),
        }
    except Exception as e:
        print(f"[POOL] トラッカー登録エラー: {e}", flush=True)
        return {"ok": False, "message": str(e)[:200]}


@router.get("/status")
async def status(current_user=Depends(get_current_user)):
    """最新の審査ジョブとプール統計"""
    job_res = (
        supabase.table("pool_jobs")
        .select("*")
        .eq("user_id", current_user.id)
        .order("started_at", desc=True)
        .limit(1)
        .execute()
    )
    counts = (
        supabase.table("watch_list")
        .select("status", count="exact")
        .eq("user_id", current_user.id)
        .eq("status", "tracking")
        .execute()
    )
    approved = (
        supabase.table("watch_list")
        .select("status", count="exact")
        .eq("user_id", current_user.id)
        .eq("status", "approved")
        .execute()
    )
    return {
        "job": job_res.data[0] if job_res.data else None,
        "tracking_count": counts.count or 0,
        "approved_count": approved.count or 0,
    }


@router.get("/list")
async def list_watch(current_user=Depends(get_current_user), limit: int = 100):
    """監視プールの中身を確認"""
    res = (
        supabase.table("watch_list")
        .select("*")
        .eq("user_id", current_user.id)
        .in_("status", ["approved", "tracking"])
        .order("sales_rank")
        .limit(limit)
        .execute()
    )
    return res.data
