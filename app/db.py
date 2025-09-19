import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "daily_words.db"

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_word (
            ymd TEXT PRIMARY KEY,
            word TEXT NOT NULL,
            definition TEXT,
            entry_id TEXT,
            lexicon_id TEXT
        );
    """)
    con.commit()
    con.close()

def get_cached(ymd: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT word, definition, entry_id, lexicon_id FROM daily_word WHERE ymd = ?", (ymd,))
    row = cur.fetchone()
    con.close()
    if row:
        return {"word": row[0], "definition": row[1], "entry_id": row[2], "lexicon_id": row[3]}
    return None

def set_cache(ymd: str, word: str, definition: str, entry_id: str, lexicon_id: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO daily_word(ymd, word, definition, entry_id, lexicon_id)
        VALUES (?, ?, ?, ?, ?)
    """, (ymd, word, definition, entry_id, lexicon_id))
    con.commit()
    con.close()
