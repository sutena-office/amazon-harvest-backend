import os
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from supabase import create_client

security = HTTPBearer()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        # 認証チェックは、DB書き込みで使い回している共有クライアントとは別に
        # 都度使い捨てのクライアントで検証する。
        # 長時間動くバックグラウンドの審査バッチが同じ共有クライアントを
        # 頻繁に使うと、認証チェックの結果が不安定になることがあるため。
        auth_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        user = auth_client.auth.get_user(token)
        return user.user
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="認証エラー。再ログインしてください。",
        )
