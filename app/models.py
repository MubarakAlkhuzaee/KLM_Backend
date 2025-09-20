from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import String, Integer, Boolean, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.sql import func
from .db_pg import Base

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), server_default=func.now())

    progress: Mapped[list["UserBattlePassProgress"]] = relationship(back_populates="user")

class BattlePass(Base):
    __tablename__ = "battle_pass"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season: Mapped[int] = mapped_column(Integer, index=True)  # e.g., 1,2,3...
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tiers: Mapped[list["BattlePassTier"]] = relationship(back_populates="battle_pass")

class BattlePassTier(Base):
    __tablename__ = "battle_pass_tier"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    battle_pass_id: Mapped[int] = mapped_column(ForeignKey("battle_pass.id", ondelete="CASCADE"), index=True)
    tier_index: Mapped[int] = mapped_column(Integer)  # 0..N
    points_required: Mapped[int] = mapped_column(Integer, default=0)

    battle_pass: Mapped["BattlePass"] = relationship(back_populates="tiers")

class UserBattlePassProgress(Base):
    __tablename__ = "user_bp_progress"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    battle_pass_id: Mapped[int] = mapped_column(ForeignKey("battle_pass.id", ondelete="CASCADE"), index=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    last_updated: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user: Mapped["User"] = relationship(back_populates="progress")

    __table_args__ = (UniqueConstraint("user_id", "battle_pass_id", name="uq_user_bp"),)

class DailyWordCache(Base):
    __tablename__ = "daily_word_cache"
    ymd: Mapped[str] = mapped_column(String(10), primary_key=True)   # YYYY-MM-DD
    word: Mapped[str] = mapped_column(String(128))
    definition: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    entry_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lexicon_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
