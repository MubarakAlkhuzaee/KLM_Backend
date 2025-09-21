from fastapi import FastAPI, Query
from pydantic import BaseModel
from datetime import date
import httpx
import os
import random

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select

from app.models import Base, DailyWordCache

DATABASE_URL = os.getenv("DATABASE_URL")
SIWAR_API_KEY = os.getenv("SIWAR_API_KEY")
LEXICON_ID = "Riyadh"

app = FastAPI()

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class DailyWord(BaseModel):
    date: str
    word: str
    definition: str | None = None
    entry_id: str | None = None
    lexicon_id: str
    source: str


@app.on_event("startup")
async def on_startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@app.get("/daily-word", response_model=DailyWord)
async def get_daily_word(refresh: bool = Query(default=False)):
    today = str(date.today())

    async with AsyncSessionLocal() as session:
        if not refresh:
            # Try cache
            cached = await session.get(DailyWordCache, today)
            if cached:
                return DailyWord(
                    date=today,
                    word=cached.word,
                    definition=cached.definition,
                    entry_id=cached.entry_id,
                    lexicon_id=cached.lexicon_id,
                    source="معجم الرياض للغة العربية المعاصرة"
                )

        # No cache or forced refresh — get random word
        random_offset = random.randint(0, 3000)

        async with httpx.AsyncClient() as client:
            res = await client.get(
                "https://siwar.ksaa.gov.sa/api/v1/external/public/search",
                params={
                    "lexiconId": LEXICON_ID,
                    "offset": random_offset,
                    "limit": 1
                },
                headers={
                    "accept": "application/json",
                    "apikey": SIWAR_API_KEY
                }
            )

        if res.status_code != 200:
            return {"detail": f"Upstream failure: {res.text}"}

        data = res.json()
        if not data or "content" not in data or not data["content"]:
            return {"detail": "Could not find a suitable word today"}

        entry = data["content"][0]
        word = entry.get("entryHead", "")
        entry_id = entry.get("entryId", None)

        # Get definition
        definition = None
        if entry_id:
            async with httpx.AsyncClient() as client:
                def_res = await client.get(
                    "https://siwar.ksaa.gov.sa/api/v1/external/public/senses",
                    params={
                        "entryId": entry_id,
                        "lexiconId": LEXICON_ID
                    },
                    headers={
                        "accept": "application/json",
                        "apikey": SIWAR_API_KEY
                    }
                )
            if def_res.status_code == 200:
                senses = def_res.json()
                if senses:
                    definition = senses[0].get("definition", {}).get("value")

        # Cache it
        obj = DailyWordCache(
            ymd=today,
            word=word,
            definition=definition,
            entry_id=entry_id,
            lexicon_id=LEXICON_ID
        )
        await session.merge(obj)
        await session.commit()

        return DailyWord(
            date=today,
            word=word,
            definition=definition,
            entry_id=entry_id,
            lexicon_id=LEXICON_ID,
            source="معجم الرياض للغة العربية المعاصرة"
        )
