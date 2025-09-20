from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
from datetime import datetime
import pytz
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db_pg import Base, engine, SessionLocal, ping
from .models import DailyWordCache, User
from .ksaa_client import KSAAClient, pick_index_for_date
from .schema import DailyWord, RegisterIn, RegisterOut
from .auth import hash_password

from fastapi.middleware.cors import CORSMiddleware

TZ = pytz.timezone("Asia/Riyadh")

app = FastAPI(title="Wordle Daily Arabic Word")

# CORS for web builds
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session

@app.on_event("startup")
async def on_startup():
    # ensure DB reachable
    await ping()
    # create tables if not exist (for simplicity; consider Alembic later)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.get("/daily-word", response_model=DailyWord)
async def daily_word(db: AsyncSession = Depends(get_db)):
    ymd = datetime.now(TZ).strftime("%Y-%m-%d")
    # check cache
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
    # fetch from KSAA
    client = KSAAClient()
    try:
        lexicon_id = await client.find_lexicon_id()
        total = await client.count_candidates(lexicon_id=lexicon_id, q="")
        if total == 0:
            raise HTTPException(status_code=502, detail="No entries found in the target lexicon")

        index = pick_index_for_date(ymd, total)
        entry = await client.get_entry_by_index(lexicon_id=lexicon_id, index=index, q="")
        entry_id = entry.get("id") or entry.get("entryId") or entry.get("uuid")
        word = entry.get("lemma") or entry.get("form") or entry.get("headword") or entry.get("word") or ""

        # pull definition
        senses = await client.get_senses(entry_id) if entry_id else []
        definition = None
        for s in senses:
            definition = (
                s.get("definition_ar") or s.get("definition") or
                s.get("gloss_ar") or s.get("gloss")
            )
            if not definition:
                defs = s.get("definitions") or s.get("definitionList")
                if isinstance(defs, list) and defs:
                    ar = next((d.get("text") for d in defs if isinstance(d, dict) and d.get("lang") in ("ar","ara","ar-SA")), None)
                    definition = ar or (defs[0].get("text") if isinstance(defs[0], dict) else defs[0])
            if definition:
                break

        # save cache
        db.add(DailyWordCache(
            ymd=ymd,
            word=word,
            definition=definition,
            entry_id=entry_id,
            lexicon_id=lexicon_id,
        ))
        await db.commit()

        return DailyWord(
            date=ymd,
            word=word,
            definition=definition,
            entry_id=entry_id,
            lexicon_id=lexicon_id
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream failure: {e}")

@app.post("/auth/register", response_model=RegisterOut)
async def register(payload: RegisterIn, db: AsyncSession = Depends(get_db)):
    user = User(email=payload.email, password_hash=hash_password(payload.password))
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Email already exists")
    await db.refresh(user)
    return RegisterOut(id=user.id, email=user.email)
