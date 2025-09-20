import hashlib
import httpx
import os
from typing import Any, Dict, List

API_BASE = os.getenv("KSAA_API_BASE", "https://siwar.ksaa.gov.sa/api/v1/external")
API_KEY = os.getenv("KSAA_API_KEY")
LEXICON_ID_ENV = os.getenv("KSAA_LEXICON_ID")
LEXICON_NAME = os.getenv("KSAA_LEXICON_NAME", "معجم الرياض للغة العربية المعاصرة")

HEADERS = {"accept": "application/json", "apikey": API_KEY}
DEFAULT_QUERY = "ا"  # <-- non-empty to satisfy APIs that disallow empty

class KSAAClient:
    def __init__(self):
        if not API_KEY:
            raise RuntimeError("KSAA_API_KEY is not set")

    async def _get(self, path: str, params: Dict[str, Any] | None = None):
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{API_BASE}{path}", headers=HEADERS, params=params)
            r.raise_for_status()
            return r.json()

    async def find_lexicon_id(self) -> str:
        if LEXICON_ID_ENV:
            return LEXICON_ID_ENV

        # Fallback: discover by name
        data = await self._get("/public/lexicons")
        items = data if isinstance(data, list) else data.get("items", [])
        for it in items:
            name = it.get("name") or it.get("title") or it.get("arName") or it.get("displayName")
            if name and LEXICON_NAME in name:
                return it.get("id") or it.get("lexiconId")
        raise RuntimeError(f"Lexicon not found: {LEXICON_NAME}")

    async def count_candidates(self, lexicon_id: str, query: str = DEFAULT_QUERY) -> int:
        # Many Swagger defs use query/lexiconId/offset/limit
        params = {
            "query": query,
            "lexiconId": lexicon_id,
            "offset": 0,
            "limit": 1
        }
        data = await self._get("/public/search", params=params)
        # Try common shapes for total:
        total = (
            data.get("total")
            or data.get("count")
            or (data.get("page", {}).get("totalElements") if isinstance(data.get("page"), dict) else None)
            or (len(data.get("items", [])) if "items" in data else None)
            or 0
        )
        return int(total) if total else 0

    async def get_entry_by_index(self, lexicon_id: str, index: int, query: str = DEFAULT_QUERY) -> Dict[str, Any]:
        params = {
            "query": query,
            "lexiconId": lexicon_id,
            "offset": index,
            "limit": 1
        }
        data = await self._get("/public/search", params=params)

        items: List[Dict[str, Any]] = []
        if isinstance(data, list):
            items = data
        elif "items" in data and isinstance(data["items"], list):
            items = data["items"]
        elif "content" in data and isinstance(data["content"], list):
            items = data["content"]

        if not items:
            raise RuntimeError("No entries returned for that index")
        return items[0]

    async def get_senses(self, entry_id: str) -> List[Dict[str, Any]]:
        # Many APIs expect 'entryId' (singular)
        params = {"entryId": entry_id}
        data = await self._get("/public/senses", params=params)
        if isinstance(data, list):
            return data
        if "items" in data and isinstance(data["items"], list):
            return data["items"]
        if "senses" in data and isinstance(data["senses"], list):
            return data["senses"]
        return []

def pick_index_for_date(ymd: str, modulo: int) -> int:
    digest = hashlib.sha256(ymd.encode("utf-8")).hexdigest()
    n = int(digest[:8], 16)
    return n % max(1, modulo)
