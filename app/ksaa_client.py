import hashlib
import httpx
import os
from typing import Any, Dict, List, Optional

API_BASE = os.getenv("KSAA_API_BASE", "https://siwar.ksaa.gov.sa/api/v1/external")
API_KEY = os.getenv("KSAA_API_KEY")  # <-- set in Railway
LEXICON_NAME = os.getenv("KSAA_LEXICON_NAME", "معجم الرياض للغة العربية المعاصرة")

HEADERS = {"accept": "application/json", "apikey": API_KEY}

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
        """
        GET /public/lexicons
        Find the lexicon whose Arabic name matches LEXICON_NAME (substring ok).
        """
        data = await self._get("/public/lexicons")
        # Expecting a list of lexicons. Adjust keys if needed.
        for item in (data if isinstance(data, list) else data.get("items", [])):
            name = item.get("name") or item.get("title") or item.get("arName")
            if name and LEXICON_NAME in name:
                return item.get("id") or item.get("lexiconId")
        raise RuntimeError(f"Lexicon not found by name: {LEXICON_NAME}")

    async def count_candidates(self, lexicon_id: str, q: str = "") -> int:
        """
        If the API has a 'total' field, call a 1-per-page search to read it.
        🔧 Adjust params to the real API:
           common patterns: q / query, lexiconIds, page, size
        """
        params = {
            "q": q,
            "lexiconIds": lexicon_id,  # sometimes comma-separated if multiple
            "page": 0,
            "size": 1
        }
        data = await self._get("/public/search", params=params)
        # Try common shapes:
        return (
            data.get("total") or
            data.get("count") or
            (data.get("page", {}).get("totalElements") if isinstance(data.get("page"), dict) else None) or
            0
        ) or 0

    async def get_entry_by_index(self, lexicon_id: str, index: int, q: str = "") -> Dict[str, Any]:
        """
        Fetch exactly one entry using paging.
        🔧 If your API uses 'offset' instead of (page,size), change accordingly.
        """
        params = {
            "q": q,
            "lexiconIds": lexicon_id,
            "page": index,   # one-based vs zero-based? If off-by-one, adjust here.
            "size": 1
        }
        data = await self._get("/public/search", params=params)

        # Normalize list of entries
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
        """
        GET /public/senses?entryId=<id>
        🔧 If your API expects a different parameter name (e.g., 'entryIds' or 'id'), adjust.
        """
        params = {"entryId": entry_id}
        data = await self._get("/public/senses", params=params)
        # Normalize to a list
        if isinstance(data, list):
            return data
        if "items" in data and isinstance(data["items"], list):
            return data["items"]
        if "senses" in data and isinstance(data["senses"], list):
            return data["senses"]
        return []

def pick_index_for_date(ymd: str, modulo: int) -> int:
    """
    Deterministic index from date string 'YYYY-MM-DD'.
    """
    digest = hashlib.sha256(ymd.encode("utf-8")).hexdigest()
    n = int(digest[:8], 16)  # 32-bit slice
    # If API uses 0-based pages, return n % modulo; else add +1.
    return n % max(1, modulo)
