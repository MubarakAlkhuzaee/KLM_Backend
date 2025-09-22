# app/main.py
from __future__ import annotations

from datetime import datetime
import secrets
import re
from typing import Optional, List, Tuple

import pytz
from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert

from .db_pg import Base, engine, SessionLocal, ping
from .models import DailyWordCache
from .ksaa_client import KSAAClient

# ───────── Config ─────────
TZ = pytz.timezone("Asia/Riyadh")
MIN_LEN, MAX_LEN = 4, 7               # playable length after removing diacritics
BATCH_SIZE = 50
MAX_RANDOM_TRIES = 12
SEED_LETTERS = [
    "ا","ب","ت","ث","ج","ح","خ","د","ذ","ر","ز",
    "س","ش","ص","ض","ط","ظ","ع","غ","ف","ق","ك","ل","م","ن","ه","و","ي"
]

# ───────── App ─────────
app = FastAPI(title="Kalam API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # tighten later
    allow_methods=["GET","POST","OPTIONS"],
    allow_headers=["*"],
)

# ───────── Pydantic models ─────────
class DailyWord(BaseModel):
    date: str
    word: str
    definition: Optional[str] = None
    entry_id: Optional[str] = None
    lexicon_id: Optional[str] = None
    source: str = "معجم الرياض للغة العربية المعاصرة"

class WordItem(BaseModel):
    word: str          # diacritics preserved
    bare: str          # diacritics removed
    length: int        # len(bare)
    definition: Optional[str] = None
    entry_id: Optional[str] = None

class WordListResponse(BaseModel):
    date: str
    lexicon_id: Optional[str] = None
    count: int
    items: List[WordItem]

# ───────── DB dependency ─────────
async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session

# ───────── Helpers (Arabic normalization + definition extraction) ─────────
_AR_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670]")  # tashkeel + dagger alif
_AR_LETTERS    = re.compile(r"^[\u0621-\u064A]+$")

def strip_diacritics(s: str) -> str:
    return _AR_DIACRITICS.sub("", s or "")

def base_len_ar(s: str) -> int:
    return len(strip_diacritics(s))

def is_ar_letters_only(s: str) -> bool:
    b = strip_diacritics(s or "")
    return bool(b) and bool(_AR_LETTERS.match(b))

def normalize_word(entry: dict) -> str:
    # common headword fields
    for k in ("lemma","form","headword","word","display","text","title","entryHead"):
        v = entry.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # sometimes nested
    form = entry.get("form")
    if isinstance(form, dict):
        for k in ("text","value","form"):
            v = form.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""

def normalize_entry_id(entry: dict) -> Optional[str]:
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

def _first_str(*vals):
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def extract_definition_from_senses(senses_payload) -> Optional[str]:
    """
    Accepts any payload returned by /public/senses:
    - list[str]                                  -> first string
    - list[dict] with key "senses": list[str]    -> first senses[0]
    - dict/list with fields like definition/gloss/representations -> best-effort Arabic
    """
    # Case A: list of strings
    if isinstance(senses_payload, list) and senses_payload and isinstance(senses_payload[0], str):
        return senses_payload[0].strip() or None

    # Case B: list of dicts (Swagger example: [{"senses": ["…"], "lemma": "…"}])
    if isinstance(senses_payload, list) and senses_payload and isinstance(senses_payload[0], dict):
        for s in senses_payload:
            if isinstance(s, dict):
                # direct strings in 'senses'
                if isinstance(s.get("senses"), list) and s["senses"]:
                    for item in s["senses"]:
                        if isinstance(item, str) and item.strip():
                            return item.strip()

                # common direct fields
                direct = _first_str(
                    s.get("definition_ar"),
                    s.get("gloss_ar"),
                    s.get("definition"),
                    s.get("gloss"),
                )
                if direct:
                    return direct

                # definition may be an object
                d = s.get("definition")
                if isinstance(d, dict):
                    text = _first_str(d.get("value"), d.get("text"))
                    if text:
                        return text

                # representation arrays
                for key in (
                    "representations",
                    "definitionRepresentations",
                    "senseDefinitionRepresentations",
                    "statementRepresentations",
                ):
                    reps = s.get(key)
                    if isinstance(reps, list) and reps:
                        # prefer Arabic
                        for r in reps:
                            if isinstance(r, dict) and (r.get("lang") in ("ar","ara","ar-SA","ar_SA","AR")):
                                text = _first_str(r.get("text"), r.get("value"))
                                if text:
                                    return text
                        # otherwise first textual
                        for r in reps:
                            if isinstance(r, dict):
                                text = _first_str(r.get("text"), r.get("value"))
                                if text:
                                    return text

                # definitions array variant
                defs = s.get("definitions") or s.get("definitionList")
                if isinstance(defs, list) and defs:
                    for d in defs:
                        if isinstance(d, dict) and (d.get("lang") in ("ar", "ara", "ar-SA", "ar_SA", "AR")):
                            text = _first_str(d.get("text"), d.get("value"))
                            if text:
                                return text
                    for d in defs:
                        if isinstance(d, dict):
                            text = _first_str(d.get("text"), d.get("value"))
                            if text:
                                return text
                        elif isinstance(d, str) and d.strip():
                            return d.strip()
        # if none matched, fall through

    # Case C: single dict (rare)
    if isinstance(senses_payload, dict):
        direct = _first_str(
            senses_payload.get("definition_ar"),
            senses_payload.get("gloss_ar"),
            senses_payload.get("definition"),
            senses_payload.get("gloss"),
        )
        if direct:
            return direct

        d = senses_payload.get("definition")
        if isinstance(d, dict):
            text = _first_str(d.get("value"), d.get("text"))
            if text:
                return text

        for key in ("representations","definitionRepresentations","senseDefinitionRepresentations","statementRepresentations"):
            reps = senses_payload.get(key)
            if isinstance(reps, list):
                for r in reps:
                    if isinstance(r, dict) and (r.get("lang") in ("ar","ara","ar-SA","ar_SA","AR")):
                        text = _first_str(r.get("text"), r.get("value"))
                        if text:
                            return text
                for r in reps:
                    if isinstance(r, dict):
                        text = _first_str(r.get("text"), r.get("value"))
                        if text:
                            return text

    return None

# ───────── Lifecycle ─────────
@app.on_event("startup")
async def on_startup():
    await ping()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

@app.get("/healthz")
async def healthz():
    return {"ok": True}

# ───────── /daily-word (random; saved once per day; refreshable) ─────────
@app.get("/daily-word", response_model=DailyWord)
async def daily_word(
    db: AsyncSession = Depends(get_db),
    refresh: bool = Query(False, description="Force a new random pick for today"),
):
    ymd = datetime.now(TZ).strftime("%Y-%m-%d")

    # Cache unless refresh=1
    if not refresh:
        res = await db.execute(select(DailyWordCache).where(DailyWordCache.ymd == ymd))
        row = res.scalar_one_or_none()
        if row:
            return DailyWord(
                date=ymd,
                word=row.word,
                definition=row.definition,
                entry_id=row.entry_id,
                lexicon_id=row.lexicon_id,
            )

    # If refresh: clear today's row
    if refresh:
        await db.execute(delete(DailyWordCache).where(DailyWordCache.ymd == ymd))
        await db.commit()

    client = KSAAClient()
    try:
        lexicon_id = await client.find_lexicon_id()
        total = await client.count_candidates(lexicon_id=lexicon_id)
        if total <= 0:
            raise HTTPException(status_code=502, detail="No entries found in the target lexicon")

        has_batch = hasattr(client, "search_batch")
        chosen: Optional[Tuple[str, Optional[str], Optional[str]]] = None  # (word, def, entry_id)

        for _attempt in range(MAX_RANDOM_TRIES):
            seed = SEED_LETTERS[secrets.randbelow(len(SEED_LETTERS))]
            start = secrets.randbelow(max(1, total))

            if has_batch:
                data = await client.search_batch(lexicon_id=lexicon_id, offset=start, limit=BATCH_SIZE, query=seed)
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = data.get("items") or data.get("content") or []
                else:
                    items = []
                best_valid = None
                for entry in items:
                    word = normalize_word(entry)
                    if not word or not is_ar_letters_only(word):
                        continue
                    if not (MIN_LEN <= base_len_ar(word) <= MAX_LEN):
                        continue
                    eid = normalize_entry_id(entry)
                    definition = None
                    if eid:
                        senses = await client.get_senses(eid, lexicon_id=lexicon_id)
                        definition = extract_definition_from_senses(senses)
                    if not definition:
                        # 🔁 fallback: query-based senses (Swagger shape)
                        senses_q = await client.get_senses_by_query(word, lexicon_id=lexicon_id)
                        definition = extract_definition_from_senses(senses_q)

                    cand = (word, definition, eid)
                    if definition:
                        chosen = cand
                        break
                    if best_valid is None:
                        best_valid = cand
                if chosen:
                    break
                if best_valid and not chosen:
                    chosen = best_valid
            else:
                # single-entry probe near random start
                probe_window = min(BATCH_SIZE, total)
                best_valid = None
                for step in range(probe_window):
                    idx = (start + step) % total
                    entry = await client.get_entry_by_index(lexicon_id=lexicon_id, index=idx, query=seed)
                    word = normalize_word(entry)
                    if not word or not is_ar_letters_only(word):
                        continue
                    if not (MIN_LEN <= base_len_ar(word) <= MAX_LEN):
                        continue
                    eid = normalize_entry_id(entry)
                    definition = None
                    if eid:
                        senses = await client.get_senses(eid, lexicon_id=lexicon_id)
                        definition = extract_definition_from_senses(senses)
                    if not definition:
                        senses_q = await client.get_senses_by_query(word, lexicon_id=lexicon_id)
                        definition = extract_definition_from_senses(senses_q)

                    cand = (word, definition, eid)
                    if definition:
                        chosen = cand
                        break
                    if best_valid is None:
                        best_valid = cand
                if chosen:
                    break
                if best_valid and not chosen:
                    chosen = best_valid

        if not chosen:
            raise HTTPException(status_code=502, detail="Could not find a suitable word today")

        word, definition, eid = chosen

        # UPSERT today's row; first writer wins
        stmt = insert(DailyWordCache).values(
            ymd=ymd, word=word, definition=definition, entry_id=eid, lexicon_id=lexicon_id
        ).on_conflict_do_nothing(index_elements=["ymd"])
        await db.execute(stmt)
        await db.commit()

        # Read back (handles race)
        res = await db.execute(select(DailyWordCache).where(DailyWordCache.ymd == ymd))
        saved = res.scalar_one()
        return DailyWord(
            date=ymd,
            word=saved.word,
            definition=saved.definition,
            entry_id=saved.entry_id,
            lexicon_id=saved.lexicon_id,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream failure: {e}")

# ───────── internal collector for /words ─────────
async def _collect_words(
    client: KSAAClient,
    lexicon_id: str,
    total: int,
    need: int,
    min_len: int,
    max_len: int,
    batch_size: int = 50,
    max_attempts: int = 30,
) -> List[Tuple[str, Optional[str], Optional[str]]]:
    """
    Collect up to `need` words as (word_with_diacritics, definition, entry_id).
    Requires a non-empty definition.
    """
    have: List[Tuple[str, Optional[str], Optional[str]]] = []
    seen: set[str] = set()
    has_batch = hasattr(client, "search_batch")

    attempts = 0
    while len(have) < need and attempts < max_attempts:
        attempts += 1
        seed = SEED_LETTERS[secrets.randbelow(len(SEED_LETTERS))]
        start = secrets.randbelow(max(1, total))

        if has_batch:
            data = await client.search_batch(lexicon_id=lexicon_id, offset=start, limit=batch_size, query=seed)
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("content") or []
            else:
                items = []
        else:
            items = []
            window = min(batch_size, total)
            for step in range(window):
                idx = (start + step) % total
                try:
                    entry = await client.get_entry_by_index(lexicon_id=lexicon_id, index=idx, query=seed)
                    items.append(entry)
                except Exception:
                    continue

        for entry in items:
            word = normalize_word(entry)  # diacritics preserved
            if not word or not is_ar_letters_only(word):
                continue
            bare = strip_diacritics(word)
            L = len(bare)
            if not (min_len <= L <= max_len):
                continue
            if word in seen:
                continue

            eid = normalize_entry_id(entry)
            definition = None
            if eid:
                senses = await client.get_senses(eid, lexicon_id=lexicon_id)
                definition = extract_definition_from_senses(senses)
            if not definition:
                senses_q = await client.get_senses_by_query(word, lexicon_id=lexicon_id)
                definition = extract_definition_from_senses(senses_q)
            if not definition:
                continue  # require a definition

            have.append((word, definition, eid))
            seen.add(word)
            if len(have) >= need:
                break

    return have

# ───────── /words (multiple words with diacritics + definitions; no DB cache) ─────────
@app.get("/words", response_model=WordListResponse)
async def list_words(
    count: int = Query(10, ge=1, le=100, description="How many words to return"),
    min_len: int = Query(4, ge=2, le=20, description="Min length after removing diacritics"),
    max_len: int = Query(7, ge=2, le=30, description="Max length after removing diacritics"),
    db: AsyncSession = Depends(get_db),
):
    if min_len > max_len:
        raise HTTPException(status_code=400, detail="min_len must be <= max_len")

    ymd = datetime.now(TZ).strftime("%Y-%m-%d")
    client = KSAAClient()

    try:
        lexicon_id = await client.find_lexicon_id()
        total = await client.count_candidates(lexicon_id=lexicon_id)
        if total <= 0:
            raise HTTPException(status_code=502, detail="No entries found in the target lexicon")

        triples = await _collect_words(
            client=client,
            lexicon_id=lexicon_id,
            total=total,
            need=count,
            min_len=min_len,
            max_len=max_len,
            batch_size=BATCH_SIZE,
            max_attempts=40,
        )

        items_out: List[WordItem] = []
        for (word, definition, eid) in triples:
            bare = strip_diacritics(word)
            items_out.append(WordItem(
                word=word,
                bare=bare,
                length=len(bare),
                definition=definition,
                entry_id=eid
            ))

        return WordListResponse(
            date=ymd,
            lexicon_id=lexicon_id,
            count=len(items_out),
            items=items_out
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream failure: {e}")

# ───────── Backfill today's definition without changing the word ─────────
@app.post("/daily-word/backfill-definition", response_model=DailyWord)
async def backfill_definition(db: AsyncSession = Depends(get_db)):
    ymd = datetime.now(TZ).strftime("%Y-%m-%d")
    res = await db.execute(select(DailyWordCache).where(DailyWordCache.ymd == ymd))
    row = res.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="No cached word for today")

    if row.definition:
        return DailyWord(date=ymd, word=row.word, definition=row.definition,
                         entry_id=row.entry_id, lexicon_id=row.lexicon_id)

    client = KSAAClient()
    lexicon_id = row.lexicon_id or await client.find_lexicon_id()
    definition = None
    if row.entry_id:
        senses = await client.get_senses(row.entry_id, lexicon_id=lexicon_id)
        definition = extract_definition_from_senses(senses)
    if not definition:
        senses_q = await client.get_senses_by_query(row.word, lexicon_id=lexicon_id)
        definition = extract_definition_from_senses(senses_q)
    if not definition:
        raise HTTPException(status_code=502, detail="Still could not find a definition upstream")

    row.definition = definition
    await db.commit()
    return DailyWord(date=ymd, word=row.word, definition=row.definition,
                     entry_id=row.entry_id, lexicon_id=lexicon_id)
