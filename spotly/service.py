from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select, text

from .models import Activity, Attempt, Event, Feedback, Invitation, Photo, uid


def participant_id(key):
    return sha256(key.encode()).hexdigest()


async def lock_form(session, key):
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
    )
    inv = await session.get(Invitation, key)
    if inv is None:
        attempt = Attempt(id=uid(), invitation_key=key, raw={})
        inv = Invitation(key=key, attempt_id=attempt.id, version=0, last_session="")
        session.add_all([attempt, inv])
        await session.flush()
    attempt = await session.get(Attempt, inv.attempt_id)
    return inv, attempt


async def state_of(session, inv, attempt):
    photos = (
        await session.scalars(
            select(Photo)
            .where(Photo.attempt_id == attempt.id, Photo.active)
            .order_by(Photo.position, Photo.created_at)
        )
    ).all()
    event = await session.scalar(select(Event).where(Event.invitation_key == inv.key))
    final_feedback = await session.scalar(
        select(Feedback.id).where(Feedback.attempt_id == attempt.id, Feedback.kind == "final")
    )
    return dict(
        participant_id=participant_id(inv.key),
        feedback_final=bool(final_feedback),
        attempt_id=attempt.id,
        version=inv.version,
        status=attempt.status,
        raw=attempt.raw,
        saved_at=attempt.saved_at.isoformat() if attempt.started_at else None,
        last_session=inv.last_session,
        event_id=event.id if event else None,
        event=event.details if event else None,
        photos=[
            dict(
                id=p.id,
                upload_id=p.upload_id,
                name=p.name,
                width=p.width,
                height=p.height,
                size=p.size,
                thumbnail=f"/api/forms/{inv.key}/photos/{p.id}/thumbnail",
                preview=f"/api/forms/{inv.key}/photos/{p.id}/preview",
            )
            for p in photos
        ],
    )


def stale(inv, attempt, body):
    return (
        inv.attempt_id != body.attempt_id
        or inv.version != body.version
        or attempt.status != "active"
    )


def mark_changed(inv, attempt, session_id, *, text_change=True):
    inv.version += 1
    if text_change:
        inv.last_session = session_id
    attempt.saved_at = datetime.now(UTC)
    if attempt.started_at is None:
        attempt.started_at = attempt.saved_at


def record(
    session, key, attempt_id, name, session_id="server", field=None, value=None, details=None
):
    session.add(
        Activity(
            id=uid(),
            participant_id=participant_id(key),
            attempt_id=attempt_id,
            session_id=session_id,
            name=name,
            field=field,
            value=value,
            details=details or {},
        )
    )
