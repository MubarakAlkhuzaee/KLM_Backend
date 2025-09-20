from pydantic import BaseModel, EmailStr

class DailyWord(BaseModel):
    date: str
    word: str
    definition: str | None = None
    entry_id: str | None = None
    lexicon_id: str | None = None
    source: str = "معجم الرياض للغة العربية المعاصرة"

class RegisterIn(BaseModel):
    email: EmailStr
    password: str

class RegisterOut(BaseModel):
    id: int
    email: EmailStr
