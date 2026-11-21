from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator

from .catalog import (
    LABEL_LIMIT,
    catalog_labels,
    find_catalog_cover,
    find_catalog_event,
    list_catalog_events,
    local_start,
)
from .models import CatalogPublication, Event, now
from .validation import normalize_url

catalog_router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
MONTHS_SHORT = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def empty_date_as_none(value: object) -> object:
    return None if value == "" else value


def price_text(kopecks: int) -> str:
    if not kopecks:
        return "Бесплатно"
    rubles, remainder = divmod(kopecks, 100)
    whole = f"{rubles:,}".replace(",", "\u00a0")
    fraction = f",{remainder:02}" if remainder else ""
    return f"{whole}{fraction}\u00a0₽"


def card_values(event: Event, publication: CatalogPublication, current_time: datetime) -> dict:
    starts = local_start(event)
    url = f"/catalog/{event.id}"
    return {
        "url": url,
        "title": event.title,
        "direction": publication.direction,
        "starts_at": starts.isoformat(),
        "day": starts.day,
        "year": starts.year,
        "month": MONTHS_SHORT[starts.month - 1],
        "clock": starts.strftime("%H:%M"),
        "ended": current_time - event.starts_at >= timedelta(minutes=event.duration_minutes),
        "starts_text": starts.strftime("%d.%m.%Y · %H:%M"),
        "place": publication.city or "Онлайн",
        "price": price_text(event.price_kopecks),
        "cover": f"{url}/cover" if publication.cover_photo_id else None,
    }


@catalog_router.get("/", name="home", response_class=HTMLResponse)
@catalog_router.get("/catalog", response_class=HTMLResponse)
async def catalog_page(
    request: Request,
    q: Annotated[str, Query(max_length=150)] = "",
    on_date: Annotated[
        date | None, BeforeValidator(empty_date_as_none), Query(alias="date")
    ] = None,
    city: Annotated[str, Query(max_length=LABEL_LIMIT)] = "",
    direction: Annotated[str, Query(max_length=LABEL_LIMIT)] = "",
):
    q, city, direction = q.strip(), city.strip(), direction.strip()
    async with request.app.state.sessions() as session:
        rows = await list_catalog_events(
            session, q=q, on_date=on_date, city=city, direction=direction
        )
        cities, directions = await catalog_labels(session)

    if city and city not in cities:
        cities.insert(0, city)
    if direction and direction not in directions:
        directions.insert(0, direction)
    current_time = now()
    events = [card_values(event, publication, current_time) for event, publication in rows]
    return templates.TemplateResponse(
        request=request,
        name="catalog.html",
        context={
            "events": events,
            "all_ended": bool(events) and all(event["ended"] for event in events),
            "filters": {
                "q": q,
                "date": on_date.isoformat() if on_date else "",
                "city": city,
                "direction": direction,
            },
            "cities": cities,
            "directions": directions,
            "has_filters": bool(q or on_date or city or direction),
        },
    )


@catalog_router.get("/catalog/{event_id}", response_class=HTMLResponse)
async def catalog_event_page(event_id: str, request: Request):
    async with request.app.state.sessions() as session:
        result = await find_catalog_event(session, event_id)
    if result is None:
        raise HTTPException(404, "Мероприятие не найдено")
    event, publication = result
    registration_url = normalize_url(event.details.get("registration_url"))
    if registration_url is None:
        raise HTTPException(404, "Мероприятие не найдено")
    return templates.TemplateResponse(
        request=request,
        name="catalog_event.html",
        context={
            "event": card_values(event, publication, now())
            | {
                "description": event.description,
                "timezone": event.timezone,
                "duration": event.duration_minutes,
                "format": "Очно" if publication.city else "Онлайн",
                "address": event.details.get("address") if publication.city else None,
                "capacity": event.capacity,
                "registration_url": registration_url,
            }
        },
    )


@catalog_router.get("/catalog/{event_id}/cover")
async def catalog_cover(event_id: str, request: Request):
    async with request.app.state.sessions() as session:
        photo = await find_catalog_cover(session, event_id)
    if photo is None:
        raise HTTPException(404)
    data_dir = request.app.state.settings.data_dir.resolve()
    path = (data_dir / photo.preview).resolve()
    if not path.is_relative_to(data_dir / "photos") or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "no-store"})
