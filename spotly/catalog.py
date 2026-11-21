from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import CatalogPublication, Event, Photo, now
from .validation import normalize_url

LABEL_LIMIT = 80


def local_start(event: Event) -> datetime:
    return event.starts_at.astimezone(ZoneInfo(event.timezone))


async def publish_event(
    session: AsyncSession,
    event_id: str,
    *,
    city: str | None,
    direction: str,
    cover: bool = False,
) -> CatalogPublication:
    event = await session.get(Event, event_id)
    if event is None:
        raise ValueError("Мероприятие не найдено")
    if normalize_url(event.details.get("registration_url")) is None:
        raise ValueError("В заявке нужна ссылка на регистрацию или контакт организатора")

    direction = direction.strip()
    if not 1 <= len(direction) <= LABEL_LIMIT:
        raise ValueError(f"Укажите направление длиной от 1 до {LABEL_LIMIT} символов")

    city = (city or "").strip() or None
    if event.details["format"] == "offline":
        if city is None or len(city) > LABEL_LIMIT:
            raise ValueError(f"Укажите город длиной от 1 до {LABEL_LIMIT} символов")
    else:
        city = None

    cover_photo_id = None
    if cover:
        cover_photo_id = await session.scalar(
            select(Photo.id)
            .where(Photo.attempt_id == event.attempt_id, Photo.active)
            .order_by(Photo.position, Photo.created_at, Photo.id)
            .limit(1)
        )
        if cover_photo_id is None:
            raise ValueError("В заявке нет фото для обложки")

    statement = insert(CatalogPublication).values(
        event_id=event_id,
        city=city,
        direction=direction,
        cover_photo_id=cover_photo_id,
        published_at=now(),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[CatalogPublication.event_id],
        set_={
            "city": statement.excluded.city,
            "direction": statement.excluded.direction,
            "cover_photo_id": statement.excluded.cover_photo_id,
            "published_at": statement.excluded.published_at,
        },
    ).returning(CatalogPublication)
    publications = await session.scalars(statement, execution_options={"populate_existing": True})
    return publications.one()


async def unpublish_event(session: AsyncSession, event_id: str) -> bool:
    removed = await session.scalar(
        delete(CatalogPublication)
        .where(CatalogPublication.event_id == event_id)
        .returning(CatalogPublication.event_id)
    )
    return removed is not None


def published_events():
    return select(Event, CatalogPublication).join(
        CatalogPublication, CatalogPublication.event_id == Event.id
    )


async def list_catalog_events(
    session: AsyncSession,
    *,
    q: str = "",
    on_date: date | None = None,
    city: str = "",
    direction: str = "",
) -> list[tuple[Event, CatalogPublication]]:
    statement = published_events()
    if q:
        statement = statement.where(Event.title.icontains(q, autoescape=True))
    if city:
        statement = statement.where(CatalogPublication.city == city)
    if direction:
        statement = statement.where(CatalogPublication.direction == direction)
    rows = await session.execute(statement.order_by(Event.starts_at, Event.id))
    return [
        (event, publication)
        for event, publication in rows
        if on_date is None or local_start(event).date() == on_date
    ]


async def catalog_labels(session: AsyncSession) -> tuple[list[str], list[str]]:
    rows = (
        await session.execute(
            select(CatalogPublication.city, CatalogPublication.direction).distinct()
        )
    ).all()
    cities = sorted({city for city, _ in rows if city}, key=str.casefold)
    directions = sorted({direction for _, direction in rows}, key=str.casefold)
    return cities, directions


async def find_catalog_event(
    session: AsyncSession, event_id: str
) -> tuple[Event, CatalogPublication] | None:
    row = (await session.execute(published_events().where(Event.id == event_id))).one_or_none()
    if row is None:
        return None
    event, publication = row
    return event, publication


async def find_catalog_cover(session: AsyncSession, event_id: str) -> Photo | None:
    return await session.scalar(
        select(Photo)
        .join(CatalogPublication, CatalogPublication.cover_photo_id == Photo.id)
        .join(Event, Event.id == CatalogPublication.event_id)
        .where(Event.id == event_id, Photo.attempt_id == Event.attempt_id, Photo.active)
    )
