# app/models.py
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String
from .db_pg import Base

class DailyWordCache(Base):
    __tablename__ = "daily_word_cache"

    # YYYY-MM-DD
    ymd: Mapped[str] = mapped_column(String(10), primary_key=True)
    word: Mapped[str] = mapped_column(String(128))
    definition: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    entry_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lexicon_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
