from fastapi import APIRouter, Depends, Query, BackgroundTasks
from database import supabase
from auth import get_current_user

router = APIRouter()


@router.get("/")
async def get_deals(
    current_user=Depends(get_current_user),
    limit: int = Query(50, le=100),
    offset: int = Query(0),
):
    """刈り取り候補一覧を取得（ASINが有効なもののみ）"""
    response = (
        supabase.table("harvest_results")
        .select("*")
        .eq("user_id", current_user.id)
        .like("amazon_asin", "B%")
        .order("found_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return response.data


@router.delete("/{deal_id}")
async def delete_deal(deal_id: str, current_user=Depends(get_current_user)):
    supabase.table("harvest_results").delete().eq("id", deal_id).eq("user_id", current_user.id).execute()
    return {"message": "削除しました"}


@router.post("/run")
async def run_harvest_now(background_tasks: BackgroundTasks, current_user=Depends(get_current_user)):
    """今すぐ刈り取りスキャンを実行"""
    from scheduler import run_harvest_for_user
    setting_res = supabase.table("harvest_settings").select("*").eq("user_id", current_user.id).execute()
    if setting_res.data:
        setting = setting_res.data[0]
    else:
        setting = {
            "user_id": current_user.id,
            "min_profit_rate": 15,
            "min_profit_amount": 500,
            "min_drop_rate": 20,
            "max_rank": 100000,
            "amazon_fee_rate": 15.4,
            "notify_enabled": False,
            "line_user_id": None,
        }
    background_tasks.add_task(run_harvest_for_user, setting)
    return {"message": "スキャンを開始しました。1〜2分後に結果が表示されます。"}
