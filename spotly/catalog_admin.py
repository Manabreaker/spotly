import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .catalog import publish_event, unpublish_event
from .config import Settings
from .db import database
from .models import CatalogPublication, Event


async def run(args: argparse.Namespace) -> str:
    engine, sessions = database(Settings().database_url)
    try:
        async with sessions.begin() as session:
            if args.action == "list":
                rows = await session.execute(
                    select(Event.id, Event.created_at, Event.title, CatalogPublication.event_id)
                    .outerjoin(CatalogPublication, CatalogPublication.event_id == Event.id)
                    .order_by(Event.created_at.desc(), Event.id)
                )
                return "\n".join(
                    f"{event_id}\t{created_at.isoformat()}\t"
                    f"{'опубликовано' if published else 'сохранено'}\t{title!r}"
                    for event_id, created_at, title, published in rows
                )
            if args.action == "publish":
                publication = await publish_event(
                    session,
                    args.event_id,
                    city=args.city,
                    direction=args.direction,
                    cover=args.cover,
                )
                return f"/catalog/{publication.event_id}"
            removed = await unpublish_event(session, args.event_id)
            return "Публикация снята" if removed else "Карточка уже закрыта"
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Управление каталогом Spotly")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="показать сохранённые заявки")
    publish = actions.add_parser("publish", help="опубликовать выбранное мероприятие")
    publish.add_argument("event_id")
    publish.add_argument("--city")
    publish.add_argument("--direction", required=True)
    publish.add_argument("--cover", action="store_true", help="показать первое фото")
    unpublish = actions.add_parser("unpublish", help="закрыть карточку")
    unpublish.add_argument("event_id")

    args = parser.parse_args(argv)
    try:
        output = asyncio.run(run(args))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print("Проверьте подключение к БД и миграции каталога", file=sys.stderr)
        return 1
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
