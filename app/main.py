from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import date
import random
import httpx
import os

from app.database import get_session, engine
from app.models import Base, DailyWordCache


app = FastAPI()

# ─────────────────────────────────────────────
# 🧱 Pydantic response model
# ─────────────────────────────────────────────
class DailyWord(BaseModel):
    date: str
    word: str
    definition: str | None = None
    entry_id: str | None = None
    lexicon_id: str | None = None
    source: str = "معجم الرياض للغة العربية المعاصرة"


# ─────────────────────────────────────────────
# 🔌 Helper to fetch random word from Siwar API
# ─────────────────────────────────────────────
async def fetch_random_word_from_siwar(query: str) -> DailyWord | None:
    headers = {
        "accept": "application/json",
        "apikey": os.getenv("SIWAR_API_KEY"),
    }

    url = "https://siwar.ksaa.gov.sa/api/v1/external/public/search"
    params = {
        "query": query,
        "lexiconId": "Riyadh",
        "offset": 0,
        "limit": 1
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers, params=params)

        if response.status_code != 200:
            raise Exception(response.text)

        data = response.json()
        if not data or "content" not in data or len(data["content"]) == 0:
            return None

        entry = data["content"][0]

        return DailyWord(
            date=str(date.today()),
            word=entry["title"],
            definition=entry.get("definition"),
            entry_id=entry.get("id"),
            lexicon_id="Riyadh",
        )


# ─────────────────────────────────────────────
# 🚀 API route: /daily-word
# ─────────────────────────────────────────────
@app.get("/daily-word", response_model=DailyWord)
async def get_daily_word(refresh: bool = False, session: AsyncSession = Depends(get_session)):
    today = date.today().isoformat()

    # Check if already exists in DB
    if not refresh:
        existing = await session.get(DailyWordCache, today)
        if existing:
            return DailyWord(
                date=existing.ymd,
                word=existing.word,
                definition=existing.definition,
                entry_id=existing.entry_id,
                lexicon_id=existing.lexicon_id
            )

    # Pick random query letter (more letters = more diversity)
    query = random.choice(["س", "م", "ن", "ك", "ر", "ب", "ط", "ع", "ف", "و", "خ", "ج"])

    try:
        word = await fetch_random_word_from_siwar(query=query)

        if word is None or not word.word:
            raise Exception("Could not find a suitable word today")

        # Save to DB
        cache_entry = DailyWordCache(
            ymd=today,
            word=word.word,
            definition=word.definition,
            entry_id=word.entry_id,
            lexicon_id=word.lexicon_id
        )
        session.add(cache_entry)
        await session.commit()

        return word

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"Upstream failure: {str(e)}"}
        )


# ─────────────────────────────────────────────
# 🌱 Create DB tables if they don't exist
# ─────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
