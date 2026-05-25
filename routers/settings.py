from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional
from database import supabase
from auth import get_current_user

router = APIRouter()

DEFAULT_SETTINGS = {
    "min_profit_rate": 15,
    "min_profit_amount": 500,
    "min_drop_rate": 20,
    "max_rank": 100000,
    "amazon_fee_rate": 15.4,
    "line_user_id": "",
    "notify_enabled": False,
}


class SettingsUpdate(BaseModel):
    min_profit_rate: Optional[int] = None
    min_profit_amount: Optional[int] = None
    min_drop_rate: Optional[float] = None
    max_rank: Optional[int] = None
    amazon_fee_rate: Optional[float] = None
    line_user_id: Optional[str] = None
    notify_enabled: Optional[bool] = None


@router.get("/")
async def get_settings(current_user=Depends(get_current_user)):
    res = supabase.table("harvest_settings").select("*").eq("user_id", current_user.id).execute()
    if res.data:
        return res.data[0]
    return {**DEFAULT_SETTINGS, "user_id": current_user.id}


@router.put("/")
async def update_settings(settings: SettingsUpdate, current_user=Depends(get_current_user)):
    update_data = {k: v for k, v in settings.model_dump().items() if v is not None}
    existing = supabase.table("harvest_settings").select("user_id").eq("user_id", current_user.id).execute()
    if existing.data:
        supabase.table("harvest_settings").update(update_data).eq("user_id", current_user.id).execute()
    else:
        supabase.table("harvest_settings").insert({**DEFAULT_SETTINGS, **update_data, "user_id": current_user.id}).execute()
    return {"message": "設定を更新しました"}
