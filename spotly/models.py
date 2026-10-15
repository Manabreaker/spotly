from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

J = JSON().with_variant(JSONB(), "postgresql")


def uid():
    return str(uuid4())


def now():
    return datetime.now(UTC)


class Invitation(Base):
    __tablename__ = "invitations"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(default=0)
    last_session: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    invitation_key: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    raw: Mapped[dict] = mapped_column(J, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    invitation_key: Mapped[str] = mapped_column(String(64), unique=True)
    attempt_id: Mapped[str] = mapped_column(String(36), unique=True)
    title: Mapped[str] = mapped_column(String(150))
    description: Mapped[str] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(100))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    price_kopecks: Mapped[int] = mapped_column(Integer)
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[dict] = mapped_column(J)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Photo(Base):
    __tablename__ = "photos"
    __table_args__ = (UniqueConstraint("invitation_key", "upload_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    invitation_key: Mapped[str] = mapped_column(String(64), index=True)
    attempt_id: Mapped[str] = mapped_column(String(36), index=True)
    upload_id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(255))
    original: Mapped[str] = mapped_column(String(300))
    preview: Mapped[str] = mapped_column(String(300))
    thumbnail: Mapped[str] = mapped_column(String(300))
    media_type: Mapped[str] = mapped_column(String(100))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    size: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (Index("ix_activity_attempt_time", "attempt_id", "occurred_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    participant_id: Mapped[str] = mapped_column(String(64), index=True)
    attempt_id: Mapped[str] = mapped_column(String(36), index=True)
    session_id: Mapped[str] = mapped_column(String(100))
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(80))
    field: Mapped[str | None] = mapped_column(String(100), nullable=True)
    value: Mapped[object | None] = mapped_column(J, nullable=True)
    details: Mapped[dict] = mapped_column(J, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        Index(
            "uq_final_feedback", "attempt_id", unique=True, postgresql_where=text("kind = 'final'")
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    invitation_key: Mapped[str] = mapped_column(String(64), index=True)
    attempt_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    comment: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
