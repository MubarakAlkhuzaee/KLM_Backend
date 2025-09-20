import hashlib, os
from typing import Any, Dict, List
import httpx

API_BASE = os.getenv("KSAA_API_BASE", "https://siwar.ksaa.gov.sa/api/v1/external")
API_KEY = os.getenv("KSAA_API_KEY")
LEXICON_ID = os.getenv("KSAA_LEXICON_ID")  # e.g., "Riyadh"
LEXICON_NAME = os.getenv("KSAA_LEXICON_NAME", "معجم الرياض للغة العربية المعاصرة")

HEADERS = {"accept": "application/json", "apikey": API_KEY}
DEFAULT_QUERY = "ا"  # ensure non-empty

def _q(q: str | None) -> str:
    return q if (q and str(q).strip()) else DEFAULT_QUERY

async def _http_get(path: str, params: Dict[str, Any]):
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{API_BASE}{path}", headers=HEADERS, params=params)
        r.raise_for_status()
        return r.json()

def _collect_total(data: Any) -> int:
    if isinstance(data, dict):
        for k in ("total", "count"):
            if k in data and isinstance(data[k], int):
                return int(data[k])
        pg = data.get("page")
        if isinstance(pg, dict) and isinstance(pg.get("totalElements"), int):
            return int(pg["totalElements"])
        if "items" in data and isinstance(data["items"], list):
            return max(1, len(data["items"]))
        if "content" in data and isinstance(data["content"], list):
            return max(1, len(data["content"]))
    elif isinstance(data, list):
        return max(1, len(data))
    return 0

def _collect_items(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            return data["items"]
        if isinstance(data.get("content"), list):
            return data["content"]
    return []

class KSAAClient:
    def __init__(self):
        if not API_KEY:
            raise RuntimeError("KSAA_API_KEY is not set")

    async def find_lexicon_id(self) -> str:
        # trust env first; if not set, resolve by name
        if LEXICON_ID:
            return LEXICON_ID
        data = await _http_get("/public/lexicons", {})
        items = data if isinstance(data, list) else data.get("items", [])
        for it in items:
            name = it.get("name") or it.get("title") or it.get("arName") or it.get("displayName")
            if name and LEXICON_NAME in name:
                return it.get("id") or it.get("lexiconId")
        raise RuntimeError(f"Lexicon not found by name: {LEXICON_NAME}")

    async def _search_try_both(self, query: str, lexicon_id: str, offset: int, limit: int):
        # Try style A: lexiconId + offset/limit
        try:
            return await _http_get("/public/search", {
                "query": _q(query), "lexiconId": lexicon_id, "offset": offset, "limit": limit
            })
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (400, 404):
                raise
        # Try style B: lexiconIds + page/size
        page = offset  # 1:1 mapping for single fetch
        return await _http_get("/public/search", {
            "query": _q(query), "lexiconIds": lexicon_id, "page": page, "size": limit
        })

    async def count_candidates(self, lexicon_id: str, query: str | None = None) -> int:
        data = await self._search_try_both(query or DEFAULT_QUERY, lexicon_id, offset=0, limit=1)
        total = _collect_total(data)
        if not total:
            # as a fallback, try to fetch more to infer count > 0
            data2 = await self._search_try_both(query or DEFAULT_QUERY, lexicon_id, offset=0, limit=10)
            total = _collect_total(data2)
        return int(total) if total else 0

    async def get_entry_by_index(self, lexicon_id: str, index: int, query: str | None = None) -> Dict[str, Any]:
        data = await self._search_try_both(query or DEFAULT_QUERY, lexicon_id, offset=index, limit=1)
        items = _collect_items(data)
        if not items:
            raise RuntimeError("No entries returned for that index")
        return items[0]

    async def get_senses(self, entry_id: str) -> List[Dict[str, Any]]:
        # Prefer entryId; if 400, retry with entryIds (array-like)
        try:
            data = await _http_get("/public/senses", {"entryId": entry_id})
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (400, 404):
                raise
            data = await _http_get("/public/senses", {"entryIds": entry_id})
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
