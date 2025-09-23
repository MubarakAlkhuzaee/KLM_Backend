# app/build_365.py
from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Dict, List, Optional, Tuple, Set

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert

from .db_pg import Base, engine, SessionLocal
from .models import YearWord

API_BASE = os.getenv("KSAA_API_BASE", "https://siwar.ksaa.gov.sa/api/v1/external")
API_KEY = os.getenv("KSAA_API_KEY")
LEXICON_ID = os.getenv("KSAA_LEXICON_ID", "Riyadh")   # you said this is correct
HEADERS = {"accept": "application/json", "apikey": API_KEY}

# What we want to build
TARGET_COUNT = int(os.getenv("YEAR_TARGET_COUNT", "365"))

# Playability constraints (after removing diacritics)
MIN_LEN = int(os.getenv("YEAR_MIN_LEN", "4"))
MAX_LEN = int(os.getenv("YEAR_MAX_LEN", "7"))

# Search tuning
PAGE_SIZE = int(os.getenv("SYNC_PAGE_SIZE", "120"))
MAX_PAGES_PER_SEED = int(os.getenv("SYNC_MAX_PAGES_PER_SEED", "2000"))
ATTEMPT_CAP = int(os.getenv("SYNC_ATTEMPT_CAP", "20000"))

# Seeds so we never send an empty query
SEED_LETTERS = [
    "ا","ب","ت","ث","ج","ح","خ","د","ذ","ر","ز",
    "س","ش","ص","ض","ط","ظ","ع","غ","ف","ق","ك","ل","م","ن","ه","و","ي"
]
SEED_PATTERNS = ["ال","الم","است","مت","ات","ون","ية","تي","ين","تر","سي","عن","مع","قد","لا","من","في"]
SEEDS: List[str] = SEED_LETTERS + SEED_PATTERNS

# Arabic helpers
_AR_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670]")  # tashkeel + dagger alif
_AR_LETTERS = re.compile(r"^[\u0621-\u064A]+$")

def strip_diacritics(s: str) -> str:
    return _AR_DIACRITICS.sub("", s or "")

def ar_letters_only(s: str) -> bool:
    b = strip_diacritics(s or "")
    return bool(b) and bool(_AR_LETTERS.match(b))

def normalize_lemma(entry: dict) -> str:
    for k in ("lemma","form","headword","word","display","text","title","entryHead"):
        v = entry.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    form = entry.get("form")
    if isinstance(form, dict):
        for k in ("text","value","form"):
            v = form.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""

def get_entry_id(entry: dict) -> Optional[str]:
    for k in ("id","entryId","lexicalEntryId","uuid","uid","eid"):
        v = entry.get(k)
        if isinstance(v, (str,int)) and str(v).strip():
            return str(v)
    meta = entry.get("meta") or entry.get("metadata") or {}
    if isinstance(meta, dict):
        for k in ("id","entryId","lexicalEntryId"):
            v = meta.get(k)
            if isinstance(v, (str,int)) and str(v).strip():
                return str(v)
    return None

def extract_definitions(payload: Any) -> List[str]:
    """
    Accept shapes you've seen:
    - [{"senses": ["…"], "lemma": "…"}] -> extract strings
    - list[str]                          -> as-is
    - dict/list with definition/gloss/representations -> best-effort
    """
    out: List[str] = []

    def first_str(*vals):
        for v in vals:
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    # list[str]
    if isinstance(payload, list) and payload and isinstance(payload[0], str):
        return [s.strip() for s in payload if isinstance(s, str) and s.strip()]

    # list[dict]
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        for s in payload:
            if not isinstance(s, dict):
                continue
            senses = s.get("senses")
            if isinstance(senses, list):
                for t in senses:
                    if isinstance(t, str) and t.strip():
                        out.append(t.strip())

            direct = first_str(s.get("definition_ar"), s.get("gloss_ar"), s.get("definition"), s.get("gloss"))
            if direct:
                out.append(direct)

            d = s.get("definition")
            if isinstance(d, dict):
                t = first_str(d.get("value"), d.get("text"))
                if t:
                    out.append(t)

            for key in ("representations","definitionRepresentations","senseDefinitionRepresentations","statementRepresentations"):
                reps = s.get(key)
                if isinstance(reps, list):
                    # prefer Arabic
                    for r in reps:
                        if isinstance(r, dict) and (r.get("lang") in ("ar","ara","ar-SA","ar_SA","AR")):
                            t = first_str(r.get("text"), r.get("value"))
                            if t:
                                out.append(t)
                    for r in reps:
                        if isinstance(r, dict):
                            t = first_str(r.get("text"), r.get("value"))
                            if t:
                                out.append(t)
        return [d for d in out if isinstance(d, str) and d.strip()]

    # dict
    if isinstance(payload, dict):
        direct = first_str(payload.get("definition_ar"), payload.get("gloss_ar"),
                           payload.get("definition"), payload.get("gloss"))
        if direct:
            out.append(direct)
        d = payload.get("definition")
        if isinstance(d, dict):
            t = first_str(d.get("value"), d.get("text"))
            if t:
                out.append(t)
        for key in ("representations","definitionRepresentations","senseDefinitionRepresentations","statementRepresentations"):
            reps = payload.get(key)
            if isinstance(reps, list):
                for r in reps:
                    if isinstance(r, dict) and (r.get("lang") in ("ar","ara","ar-SA","ar_SA","AR")):
                        t = first_str(r.get("text"), r.get("value"))
                        if t:
                            out.append(t)
                for r in reps:
                    if isinstance(r, dict):
                        t = first_str(r.get("text"), r.get("value"))
                        if t:
                            out.append(t)

    return [d for d in out if isinstance(d, str) and d.strip()]

# HTTP helpers
def _safe_query(q: Optional[str]) -> str:
    return q if (q and str(q).strip()) else "ا"

async def _get_json(client: httpx.AsyncClient, path: str, params: Dict[str, Any]):
    r = await client.get(f"{API_BASE}{path}", headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()

async def search_page(client: httpx.AsyncClient, query: str, page: int) -> List[dict]:
    data = await _get_json(client, "/public/search", {
        "query": _safe_query(query),
        "lexiconIds": LEXICON_ID,
        "page": page,
        "size": PAGE_SIZE,
    })
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            return data["items"]
        if isinstance(data.get("content"), list):
            return data["content"]
    return []

async def senses_by_query(client: httpx.AsyncClient, lemma: str) -> Any:
    return await _get_json(client, "/public/senses", {
        "query": lemma,
        "lexiconIds": LEXICON_ID,
    })

async def senses_by_entry(client: httpx.AsyncClient, entry_id: str) -> Any:
    # try a couple of shapes
    try:
        return await _get_json(client, "/public/senses", {"entryId": entry_id, "lexiconId": LEXICON_ID})
    except httpx.HTTPStatusError as e:
        if e.response.status_code not in (400, 404):
            raise
    try:
        return await _get_json(client, "/public/senses", {"entryIds": entry_id, "lexiconId": LEXICON_ID})
    except httpx.HTTPStatusError as e:
        if e.response.status_code not in (400, 404):
            raise
    return []

# DB helpers
async def clear_table(session: AsyncSession):
    # Start fresh each run
    await session.execute(func.now())  # no-op to ensure connection
    await session.execute(f"TRUNCATE TABLE {YearWord.__tablename__} RESTART IDENTITY;")

async def upsert_year_word(session: AsyncSession, idx: int, word: str, bare: str, length: int,
                           entry_id: Optional[str], lexicon_id: str,
                           senses: List[str]):
    # We store the first sense as the 'definition' and keep all as JSON
    definition = senses[0] if senses else None

    stmt = insert(YearWord).values(
        day_index=idx,
        word=word,
        bare=bare,
        length=length,
        entry_id=entry_id,
        lexicon_id=lexicon_id,
        definition=definition,
        senses=senses or None,
    ).on_conflict_do_update(
        index_elements=[YearWord.day_index],
        set_={
            "word": word,
            "bare": bare,
            "length": length,
            "entry_id": entry_id,
            "lexicon_id": lexicon_id,
            "definition": definition,
            "senses": senses or None,
        }
    )
    await session.execute(stmt)

async def main():
    if not API_KEY:
        raise RuntimeError("KSAA_API_KEY is not set")
    # Ensure tables exist
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Build list until we reach TARGET_COUNT
    used_words: Set[str] = set()
    used_eids: Set[str] = set()
    collected: List[Tuple[str, str, int, Optional[str], List[str]]] = []  # word, bare, len, entry_id, senses
    attempts = 0

    timeout = httpx.Timeout(30.0)
    async with SessionLocal() as session, httpx.AsyncClient(timeout=timeout) as client:
        # optional: clear previous plan
        await clear_table(session)
        await session.commit()

        while len(collected) < TARGET_COUNT and attempts < ATTEMPT_CAP:
            attempts += 1
            seed = SEEDS[attempts % len(SEEDS)]
            page = (attempts // len(SEEDS)) % MAX_PAGES_PER_SEED

            items = await search_page(client, seed, page)
            if not items:
                continue

            for entry in items:
                if len(collected) >= TARGET_COUNT:
                    break

                word = normalize_lemma(entry)
                if not word or not ar_letters_only(word):
                    continue
                bare = strip_diacritics(word)
                L = len(bare)
                if L < MIN_LEN or L > MAX_LEN:
                    continue
                if word in used_words:
                    continue

                eid = get_entry_id(entry)
                if eid and eid in used_eids:
                    continue

                # senses: query-based first (Swagger-proven), then entry fallback
                senses: List[str] = []
                try:
                    p_q = await senses_by_query(client, word)
                    senses = extract_definitions(p_q)
                except Exception:
                    senses = []
                if not senses and eid:
                    try:
                        p_e = await senses_by_entry(client, eid)
                        senses = extract_definitions(p_e)
                    except Exception:
                        senses = []

                if not senses:
                    continue

                collected.append((word, bare, L, eid, senses))
                used_words.add(word)
                if eid:
                    used_eids.add(eid)

            # flush to DB in chunks of ~50
            if len(collected) and (len(collected) % 50 == 0):
                for i, (w, b, L, eid, senses) in enumerate(collected, start=1):
                    await upsert_year_word(session, i, w, b, L, eid, LEXICON_ID, senses)
                await session.commit()
                print(f"[build_365] staged {len(collected)}/{TARGET_COUNT}")

        # final commit for any remainder
        for i, (w, b, L, eid, senses) in enumerate(collected, start=1):
            await upsert_year_word(session, i, w, b, L, eid, LEXICON_ID, senses)
        await session.commit()

        if len(collected) < TARGET_COUNT:
            print(f"[build_365] WARNING: only collected {len(collected)}/{TARGET_COUNT}")
        else:
            print(f"[build_365] DONE: collected {len(collected)} words.")

if __name__ == "__main__":
    asyncio.run(main())
