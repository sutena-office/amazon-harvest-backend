import threading
import re
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from database import supabase
from auth import get_current_user

router = APIRouter()


class SeedInput(BaseModel):
    asin_or_url: str


def _extract_asin(text: str) -> str:
    """ASIN直接入力にもAmazon URL貼り付けにも対応"""
    text = text.strip()
    m = re.search(r"/dp/([A-Z0-9]{10})", text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    m = re.search(r"\b([A-Z0-9]{10})\b", text, re.IGNORECASE)
    return m.group(1).upper() if m else ""


@router.post("/seed")
async def register_seed(payload: SeedInput, current_user=Depends(get_current_user)):
    """モデル商品（種）を登録し、類似商品の発掘バッチを開始する"""
    from research.yahoo_api import is_configured
    if not is_configured():
        return {"started": False, "message": "Yahoo! Client IDが未設定です（Render環境変数 YAHOO_CLIENT_ID）"}

    asin = _extract_asin(payload.asin_or_url)
    if not asin:
        return {"started": False, "message": "ASINが読み取れません（例: B08XYZ1234 またはAmazonの商品URL）"}

    try:
        running = (
            supabase.table("sourcing_seeds")
            .select("id")
            .eq("user_id", current_user.id)
            .eq("status", "running")
            .execute()
        )
        if running.data:
            return {"started": False, "message": "発掘ジョブが既に実行中です"}

        from research.sourcing import analyze_seed
        traits = analyze_seed(asin)
        if not traits:
            return {"started": False, "message": "商品情報を取得できません（トークン切れ or ASIN不正）"}

        seed = (
            supabase.table("sourcing_seeds")
            .insert({
                "user_id": current_user.id,
                "asin": asin,
                "title": traits["title"],
                "brand": traits["brand"],
                "root_category": traits["root_category"],
                "reference_price": traits["reference_price"],
                "status": "running",
            })
            .execute()
        )
        seed_id = seed.data[0]["id"]
    except Exception as e:
        print(f"[SOURCING] 種登録エラー: {e}", flush=True)
        return {"started": False, "message": f"エラー: {str(e)[:200]}"}

    from research.sourcing import run_sourcing_job
    thread = threading.Thread(
        target=run_sourcing_job,
        args=(seed_id, current_user.id, traits),
        daemon=True,
    )
    thread.start()

    return {
        "started": True,
        "seed": {"asin": asin, "title": traits["title"], "brand": traits["brand"]},
        "message": f"「{traits['title'][:40]}」を種にして類似商品の発掘を開始しました",
    }


@router.get("/seeds")
async def list_seeds(current_user=Depends(get_current_user)):
    res = (
        supabase.table("sourcing_seeds")
        .select("*")
        .eq("user_id", current_user.id)
        .order("created_at", desc=True)
        .limit(10)
        .execute()
    )
    return res.data


@router.get("/candidates")
async def list_candidates(current_user=Depends(get_current_user), limit: int = 100):
    """発掘済み候補を利益額の大きい順に返す"""
    res = (
        supabase.table("sourcing_candidates")
        .select("*")
        .eq("user_id", current_user.id)
        .order("profit_amount", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data


@router.post("/rescan")
async def rescan(current_user=Depends(get_current_user)):
    """Yahoo!側の価格・ポイントを再チェック（無料・トークン消費なし）"""
    from research.sourcing import rescan_yahoo_prices
    thread = threading.Thread(
        target=rescan_yahoo_prices,
        args=(current_user.id, True),
        daemon=True,
    )
    thread.start()
    return {"started": True, "message": "Yahoo!価格の再スキャンを開始しました（数分で完了）"}


@router.delete("/candidate/{candidate_id}")
async def delete_candidate(candidate_id: str, current_user=Depends(get_current_user)):
    supabase.table("sourcing_candidates").delete().eq("id", candidate_id).eq(
        "user_id", current_user.id
    ).execute()
    return {"ok": True}
