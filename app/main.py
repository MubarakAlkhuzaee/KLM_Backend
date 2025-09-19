from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime
import pytz
from .db import init_db, get_cached, set_cache
from .ksaa_client import KSAAClient, pick_index_for_date

TZ = pytz.timezone("Asia/Riyadh")

app = FastAPI(title="Wordle Daily Arabic Word")

class DailyWord(BaseModel):
    date: str               # YYYY-MM-DD (Riyadh)
    word: str
    definition: str | None = None
    entry_id: str | None = None
    lexicon_id: str | None = None
    source: str = "معجم الرياض للغة العربية المعاصرة"

@app.on_event("startup")
def _startup():
    init_db()

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/daily-word", response_model=DailyWord)
async def daily_word():
    # Get today's date in Riyadh
    now_riyadh = datetime.now(TZ)
    ymd = now_riyadh.strftime("%Y-%m-%d")

    # Cache hit?
    cached = get_cached(ymd)
    if cached:
        return DailyWord(
            date=ymd,
            word=cached["word"],
            definition=cached["definition"],
            entry_id=cached["entry_id"],
            lexicon_id=cached["lexicon_id"]
        )

    # Otherwise fetch from KSAA
    client = KSAAClient()
    try:
        lexicon_id = await client.find_lexicon_id()
        total = await client.count_candidates(lexicon_id=lexicon_id, q="")  # you can scope q (e.g., by length) if API supports
        if total == 0:
            raise HTTPException(status_code=502, detail="No entries found in the target lexicon")

        index = pick_index_for_date(ymd, total)
        entry = await client.get_entry_by_index(lexicon_id=lexicon_id, index=index, q="")

        # Try to normalize fields
        entry_id = (
            entry.get("id") or
            entry.get("entryId") or
            entry.get("uuid")
        )
        # Prefer lemma/form/display fields commonly used in lexica
        word = entry.get("lemma") or entry.get("form") or entry.get("headword") or entry.get("word") or ""

        # Pull senses to get a definition in Arabic
        senses = await client.get_senses(entry_id) if entry_id else []
        definition: str | None = None

        # Look for Arabic definition fields
        for s in senses:
            definition = (
                s.get("definition_ar") or
                s.get("definition") or
                s.get("gloss_ar") or
                s.get("gloss")
            )
            # Some APIs wrap definitions under nested objects/arrays:
            if not definition:
                defs = s.get("definitions") or s.get("definitionList")
                if isinstance(defs, list) and defs:
                    # Prefer Arabic if present
                    # Example shapes:
                    # { "text": "...", "lang": "ar" } or strings
                    ar = next((d.get("text") for d in defs if isinstance(d, dict) and d.get("lang") in ("ar", "ara", "ar-SA")), None)
                    definition = ar or (defs[0].get("text") if isinstance(defs[0], dict) else defs[0])
            if definition:
                break

        if not word:
            raise HTTPException(status_code=502, detail="Entry did not contain a word/lemma field")

        # Save cache and return
        set_cache(ymd, word, definition, entry_id or "", lexicon_id)
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
