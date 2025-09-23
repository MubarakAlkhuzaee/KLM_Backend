# app/models.py
from __future__ import annotations
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Text, Integer
from sqlalchemy.dialects.postgresql import JSONB
from .db_pg import Base

# Existing table (keep if you already had it)
class DailyWordCache(Base):
    __tablename__ = "daily_word_cache"
    ymd: Mapped[str] = mapped_column(String(10), primary_key=True)
    word: Mapped[str] = mapped_column(String(128))
    definition: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    entry_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lexicon_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

# NEW: persistent 365-word plan (1..365)
class YearWord(Base):
    __tablename__ = "year_word"

    # day index in [1..365] — use 366 if you want a leap slot
    day_index: Mapped[int] = mapped_column(Integer, primary_key=True)

    # surface word (with diacritics), and bare (diacritics removed)
    word: Mapped[str] = mapped_column(String(256), nullable=False)
    bare: Mapped[str] = mapped_column(String(256), nullable=False)
    length: Mapped[int] = mapped_column(Integer, nullable=False)

    # external references (optional)
    entry_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lexicon_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # primary definition and all senses as JSON list of strings
    definition: Mapped[str | None] = mapped_column(Text, nullable=True)
    senses: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
