import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def get_client() -> Client:
    """独立したSupabaseクライアントを新規作成する。
    バックグラウンドで動く長時間処理（審査バッチ等）は、この関数で
    専用クライアントを作り、APIリクエスト処理用の共有supabaseとは
    分離して使うことで、互いの負荷が干渉しないようにする。"""
    return create_client(SUPABASE_URL, SUPABASE_KEY)
