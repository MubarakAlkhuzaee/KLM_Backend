# app/main.py
from datetime import datetime
import pytz
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# --- create app FIRST ---
app = FastAPI(title="Wordle Daily Arabic Word")

# CORS (needed if you call from browser / Godot Web)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # lock down to your domain(s) later
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# --- imports that depend on app existing are fine after this ---
from .db_pg import Base, engine, SessionLocal, ping           # Postgres helpers
from .models import DailyWordCache                            # SQLAlchemy models
from .ksaa_client import (                                    # Upstream client + helpers
    KSAAClient, pick_index_for_date,
    base_len_ar, is_ar_letters_only, normalize_entry_id, normalize_word
)

TZ = pytz.timezone("Asia/Riyadh")
MIN_LEN, MAX_LEN = 4, 7         # Wordle-like length
MAX_PROBES = 40                 # How many entries to try to find a good one

# ---- Pydantic response models ----
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

    # 1) cache
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

    # 2) fetch from upstream and probe until a playable word
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
                for s in senses:
                    definition = (
                        s.get("definition_ar") or s.get("definition") or
                        s.get("gloss_ar") or s.get("gloss")
                    )
                    if not definition:
                        defs = s.get("definitions") or s.get("definitionList")
                        if isinstance(defs, list) and defs:
                            ar = next((d.get("text") for d in defs
                                       if isinstance(d, dict) and d.get("lang") in ("ar","ara","ar-SA")), None)
                            definition = ar or (defs[0].get("text") if isinstance(defs[0], dict) else defs[0])
                    if definition:
                        break

            # accept this word; prefer ones with definition
            chosen_word, chosen_def, chosen_eid = word, definition, eid
            if definition:
                break

        if not chosen_word:
            raise HTTPException(status_code=502, detail="Could not find a suitable word today")

        # 3) save cache
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
