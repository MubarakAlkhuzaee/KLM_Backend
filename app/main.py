# app/main.py
from datetime import datetime
import re
import pytz
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# --- create app FIRST ---
app = FastAPI(title="Wordle Daily Arabic Word")

# CORS (enable if calling from browser / Godot Web; restrict origins later)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# --- project imports (after app is created is fine) ---
from .db_pg import Base, engine, SessionLocal, ping
from .models import DailyWordCache
from .ksaa_client import KSAAClient, pick_index_for_date

# ---- constants ----
TZ = pytz.timezone("Asia/Riyadh")
MIN_LEN, MAX_LEN = 4, 7        # Wordle-like length after stripping diacritics
MAX_PROBES = 60                # try up to N entries to find a good one

# ---- local helpers (don’t rely on upstream shapes for validation) ----
_AR_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670]")  # tashkeel + dagger alif
_AR_LETTERS    = re.compile(r"^[\u0621-\u064A]+$")     # Hamza..Yeh

def strip_diacritics(s: str) -> str:
    return _AR_DIACRITICS.sub("", s or "")

def base_len_ar(s: str) -> int:
    return len(strip_diacritics(s))

def is_ar_letters_only(s: str) -> bool:
    b = strip_diacritics(s or "")
    return bool(b) and bool(_AR_LETTERS.match(b))

def normalize_word(entry: dict) -> str:
    # common headword fields
    for k in ("lemma", "form", "headword", "word", "display", "text", "title"):
        v = entry.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # sometimes nested like form.text
    form = entry.get("form")
    if isinstance(form, dict):
        for k in ("text", "value", "form"):
            v = form.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""

def normalize_entry_id(entry: dict):
    # common id keys
    for k in ("id", "entryId", "lexicalEntryId", "uuid", "uid", "eid"):
        v = entry.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v)
    meta = entry.get("meta") or entry.get("metadata") or {}
    if isinstance(meta, dict):
        for k in ("id", "entryId", "lexicalEntryId"):
            v = meta.get(k)
            if isinstance(v, (str, int)) and str(v).strip():
                return str(v)
    return None

# ---- Pydantic response model (BACK!) ----
class DailyWord(BaseModel):
    date: str
    word: str
    definition: str | None = None
    entry_id: str | None = None
    lexicon_id: str | None = None
    source: str = "معجم الرياض للغة العربية المعاصرة"

# ---- DB dependency ----
async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session

# ---- lifecycle ----
@app.on_event("startup")
async def on_startup():
    await ping()  # ensure DB reachable
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# ---- routes ----
@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.get("/daily-word", response_model=DailyWord)
async def daily_word(db: AsyncSession = Depends(get_db)):
    ymd = datetime.now(TZ).strftime("%Y-%m-%d")

    # 1) cache hit?
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

    # 2) probe upstream until a playable word (prefer with definition)
    client = KSAAClient()
    try:
        lexicon_id = await client.find_lexicon_id()
        total = await client.count_candidates(lexicon_id=lexicon_id)
        if total == 0:
            raise HTTPException(status_code=502, detail="No entries found in the target lexicon")

        start = pick_index_for_date(ymd, total)

        chosen_word = None
        chosen_def  = None
        chosen_eid  = None

        for step in range(MAX_PROBES):
            idx = (start + step) % total
            entry = await client.get_entry_by_index(lexicon_id=lexicon_id, index=idx)

            word = normalize_word(entry)
            if not word or not is_ar_letters_only(word):
                continue

            L = base_len_ar(word)
            if not (MIN_LEN <= L <= MAX_LEN):
                continue

            eid = normalize_entry_id(entry)
            definition = None
            if eid:
                senses = await client.get_senses(eid)
                # Try to extract Arabic definition
                for s in senses:
                    definition = (
                        s.get("definition_ar") or
                        s.get("definition") or
                        s.get("gloss_ar") or
                        s.get("gloss")
                    )
                    if not definition:
                        defs = s.get("definitions") or s.get("definitionList")
                        if isinstance(defs, list) and defs:
                            ar = next((d.get("text") for d in defs
                                       if isinstance(d, dict) and d.get("lang") in ("ar", "ara", "ar-SA")), None)
                            definition = ar or (defs[0].get("text") if isinstance(defs[0], dict) else defs[0])
                    if definition:
                        break

            # accept candidate; prefer one with definition
            chosen_word, chosen_def, chosen_eid = word, definition, eid
            if definition:
                break

        if not chosen_word:
            raise HTTPException(status_code=502, detail="Could not find a suitable word today")

        # 3) save cache in DB
        db.add(DailyWordCache(
            ymd=ymd,
            word=chosen_word,
            definition=chosen_def,
            entry_id=chosen_eid,
            lexicon_id=lexicon_id,
        ))
        await db.commit()

        return DailyWord(
            date=ymd,
            word=chosen_word,
            definition=chosen_def,
            entry_id=chosen_eid,
            lexicon_id=lexicon_id,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream failure: {e}")
