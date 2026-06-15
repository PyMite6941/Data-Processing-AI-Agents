import os
from supabase import acreate_client, AsyncClient

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

_client: AsyncClient | None = None


async def get_db() -> AsyncClient:
    global _client
    if _client is None:
        _client = await acreate_client(SUPABASE_URL, SUPABASE_KEY)
    return _client
