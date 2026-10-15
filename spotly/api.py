import json
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .models import Activity, Attempt, Event, Feedback, uid
from .service import lock_form, mark_changed, participant_id, record, stale, state_of
from .validation import validate_event

router = APIRouter(prefix="/api/forms")
RAW_FIELDS = {
    "title",
    "description",
    "date",
    "time",
    "timezone",
    "hours",
    "minutes",
    "format",
    "address",
    "join_url",
    "join_later",
    "participation",
    "price",
    "capacity_mode",
    "capacity",
    "registration_url",
}


class Change(BaseModel):
    attempt_id: str = Field(max_length=36)
    version: int = Field(ge=0)
    session_id: str = Field(default="", max_length=100)
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("raw")
    @classmethod
    def bounded(cls, value):
        if set(value) - RAW_FIELDS or len(json.dumps(value, ensure_ascii=False)) > 100000:
            raise ValueError("invalid draft")
        if any(not isinstance(v, (str, bool, int, float, type(None))) for v in value.values()):
            raise ValueError("invalid draft value")
        return value


class BrowserEvent(BaseModel):
    id: str = Field(max_length=36)
    attempt_id: str = Field(max_length=36)
    session_id: str = Field(max_length=100)
    sequence: int = Field(default=0, ge=0)
    name: str = Field(min_length=1, max_length=80)
    field: str | None = Field(default=None, max_length=100)
    value: Any = None
    details: dict = Field(default_factory=dict)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("timezone required")
        return value


class Batch(BaseModel):
    events: list[BrowserEvent] = Field(max_length=100)

    @field_validator("events")
    @classmethod
    def bound_batch(cls, value):
        if (
            len(
                json.dumps(
                    {"events": [v.model_dump(mode="json") for v in value]},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            > 1000000
        ):
            raise ValueError("batch too large")
        return value


class FeedbackInput(BaseModel):
    id: str = Field(default_factory=uid, min_length=1, max_length=36)
    attempt_id: str = Field(max_length=36)
    kind: Literal["difficulty", "final"]
    comment: str = Field(default="", max_length=2000)
    score: int | None = Field(default=None, ge=1, le=10)


def check_key(key):
    if not 1 <= len(key) <= 64:
        raise HTTPException(404, "Ссылка недоступна")


async def conflict(session, inv, attempt):
    return JSONResponse(
        {
            "code": "conflict",
            "message": "Форма изменилась в другом окне",
            "state": await state_of(session, inv, attempt),
        },
        status_code=409,
    )


@router.get("/{key}")
async def get_form(key: str, request: Request):
    check_key(key)
    async with request.app.state.sessions() as session, session.begin():
        inv, attempt = await lock_form(session, key)
        return await state_of(session, inv, attempt)


@router.put("/{key}/draft")
async def save_draft(key: str, body: Change, request: Request):
    check_key(key)
    async with request.app.state.sessions() as session, session.begin():
        inv, attempt = await lock_form(session, key)
        if stale(inv, attempt, body):
            if (
                attempt.status == "active"
                and attempt.id == body.attempt_id
                and attempt.raw == body.raw
            ):
                return await state_of(session, inv, attempt)
            return await conflict(session, inv, attempt)
        attempt.raw = body.raw
        mark_changed(inv, attempt, body.session_id)
        await session.flush()
        return await state_of(session, inv, attempt)


@router.post("/{key}/restart")
async def restart(key: str, body: Change, request: Request):
    check_key(key)
    async with request.app.state.sessions() as session, session.begin():
        inv, attempt = await lock_form(session, key)
        if stale(inv, attempt, body):
            return await conflict(session, inv, attempt)
        if body.raw:
            attempt.raw = body.raw
            if attempt.started_at is None:
                attempt.started_at = datetime.now(UTC)
        attempt.status, attempt.ended_at = "restarted", datetime.now(UTC)
        record(session, key, attempt.id, "restart_confirmed", body.session_id, value=attempt.raw)
        fresh = Attempt(id=uid(), invitation_key=key, raw={})
        session.add(fresh)
        inv.attempt_id = fresh.id
        inv.version += 1
        inv.last_session = body.session_id
        await session.flush()
        return await state_of(session, inv, fresh)


@router.post("/{key}/submit")
async def submit(key: str, body: Change, request: Request):
    check_key(key)
    async with request.app.state.sessions() as session, session.begin():
        inv, attempt = await lock_form(session, key)
        if attempt.status == "completed":
            return await state_of(session, inv, attempt)
        if stale(inv, attempt, body):
            return await conflict(session, inv, attempt)
        attempt.raw = body.raw
        mark_changed(inv, attempt, body.session_id)
        data, errors = validate_event(body.raw)
        if errors:
            for field, message in errors.items():
                record(
                    session,
                    key,
                    attempt.id,
                    "server_validation_error",
                    body.session_id,
                    field,
                    body.raw.get(field),
                    {"message": message},
                )
            await session.flush()
            return JSONResponse(
                {
                    "code": "validation",
                    "errors": errors,
                    "state": await state_of(session, inv, attempt),
                },
                status_code=422,
            )
        event = Event(
            invitation_key=key,
            attempt_id=attempt.id,
            title=data["title"],
            description=data["description"],
            starts_at=datetime.fromisoformat(data["starts_at"]),
            timezone=data["timezone"],
            duration_minutes=data["duration_minutes"],
            price_kopecks=data["price_kopecks"],
            capacity=data["capacity"],
            details=data,
        )
        session.add(event)
        attempt.status, attempt.ended_at = "completed", datetime.now(UTC)
        record(session, key, attempt.id, "event_created", body.session_id)
        await session.flush()
        return await state_of(session, inv, attempt)


@router.post("/{key}/events")
async def events(key: str, body: Batch, request: Request):
    check_key(key)
    async with request.app.state.sessions() as session, session.begin():
        ids = set(e.attempt_id for e in body.events)
        owned = set(
            (
                await session.scalars(
                    select(Attempt.id).where(Attempt.invitation_key == key, Attempt.id.in_(ids))
                )
            ).all()
        )
        if ids - owned:
            raise HTTPException(403, "Попытка не принадлежит этой ссылке")
        values = [dict(**e.model_dump(), participant_id=participant_id(key)) for e in body.events]
        if not values:
            return {"accepted": 0}
        result = await session.execute(
            insert(Activity).values(values).on_conflict_do_nothing().returning(Activity.id)
        )
        return {"accepted": len(result.all())}


@router.post("/{key}/feedback")
async def feedback(key: str, body: FeedbackInput, request: Request):
    check_key(key)
    async with request.app.state.sessions() as session, session.begin():
        inv, current = await lock_form(session, key)
        attempt = await session.get(Attempt, body.attempt_id)
        if not attempt or attempt.invitation_key != key:
            raise HTTPException(403, "Попытка не принадлежит этой ссылке")
        if body.kind == "final" and attempt.status != "completed":
            raise HTTPException(409, "Сначала создайте мероприятие")
        result = await session.execute(
            insert(Feedback)
            .values(invitation_key=key, **body.model_dump())
            .on_conflict_do_nothing()
            .returning(Feedback.id)
        )
        if result.scalar():
            record(
                session,
                key,
                attempt.id,
                f"feedback_{body.kind}",
                value={"score": body.score, "comment": body.comment},
            )
        return {"saved": True}
