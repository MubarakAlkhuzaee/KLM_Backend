# ...existing imports...
from .ksaa_client import KSAAClient, pick_index_for_date, base_len_ar, is_ar_letters_only, normalize_entry_id, normalize_word

MAX_PROBES = 40          # how many entries to try before giving up
MIN_LEN, MAX_LEN = 4, 7  # Wordle-like length range (after stripping diacritics)

@app.get("/daily-word", response_model=DailyWord)
async def daily_word(db: AsyncSession = Depends(get_db)):
    ymd = datetime.now(TZ).strftime("%Y-%m-%d")

    # 1) check DB cache
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
            if not word:
                continue

            if not is_ar_letters_only(word):
                continue

            L = base_len_ar(word)
            if not (MIN_LEN <= L <= MAX_LEN):
                continue

            eid = normalize_entry_id(entry)
            definition = None
            if eid:
                senses = await client.get_senses(eid)
                # Try to pull Arabic definition
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
                                       if isinstance(d, dict) and d.get("lang") in ("ar","ara","ar-SA")), None)
                            definition = ar or (defs[0].get("text") if isinstance(defs[0], dict) else defs[0])
                    if definition:
                        break

            # Accept if we have a decent word; definition optional but preferred
            chosen_word = word
            chosen_def  = definition
            chosen_eid  = eid
            if definition:
                break  # prefer first with definition

        if not chosen_word:
            raise HTTPException(status_code=502, detail="Could not find a suitable word today")

        # Save cache
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
            lexicon_id=lexicon_id
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream failure: {e}")
