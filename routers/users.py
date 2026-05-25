from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from database import supabase

router = APIRouter()


class LoginRequest(BaseModel):
    email: str
    password: str


class SignupRequest(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login(req: LoginRequest):
    try:
        res = supabase.auth.sign_in_with_password({"email": req.email, "password": req.password})
        return {"access_token": res.session.access_token, "user_id": res.user.id}
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"ログイン失敗: {str(e)}")


@router.post("/signup")
async def signup(req: SignupRequest):
    try:
        res = supabase.auth.sign_up({"email": req.email, "password": req.password})
        return {"message": "登録完了。メールを確認してください。", "user_id": res.user.id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"登録失敗: {str(e)}")
