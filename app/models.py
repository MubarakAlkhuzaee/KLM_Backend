from sqlalchemy.orm import declarative_base, mapped_column
from sqlalchemy import String

Base = declarative_base()

class DailyWordCache(Base):
    __tablename__ = "daily_word_cache"

    ymd = mapped_column(String(10), primary_key=True)  # Format: YYYY-MM-DD
    word = mapped_column(String(128))
    definition = mapped_column(String(2000), nullable=True)
    entry_id = mapped_column(String(128), nullable=True)
    lexicon_id = mapped_column(String(128), nullable=True)
