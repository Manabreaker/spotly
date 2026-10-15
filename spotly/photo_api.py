import asyncio
import json
import re
import sys
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import Field
from sqlalchemy import select

from .api import Change, check_key, conflict
from .models import Photo
from .service import lock_form, mark_changed, record, stale, state_of

router = APIRouter(prefix="/api/forms")
MAX_BYTES = 30_000_000


class Order(Change):
    photo_ids: list[str] = Field(max_length=5)


async def count_active(session, attempt_id):
    return list(
        (
            await session.scalars(
                select(Photo)
                .where(Photo.attempt_id == attempt_id, Photo.active)
                .order_by(Photo.position)
            )
        ).all()
    )


@router.post("/{key}/photos")
async def upload_photo(
    key: str,
    request: Request,
    attempt_id: str = Form(max_length=36),
    session_id: str = Form(max_length=100),
    upload_id: str = Form(max_length=36),
    file: UploadFile = File(),
):
    check_key(key)
    async with request.app.state.sessions() as session, session.begin():
        inv, attempt = await lock_form(session, key)
        existing = await session.scalar(
            select(Photo).where(Photo.invitation_key == key, Photo.upload_id == upload_id)
        )
        if existing:
            return await state_of(session, inv, attempt)
        if attempt.id != attempt_id or attempt.status != "active":
            return await conflict(session, inv, attempt)
        if len(await count_active(session, attempt.id)) >= 5:
            raise HTTPException(422, "Можно добавить не больше пяти фотографий")
    photo_id = str(uuid4())
    directory = request.app.state.settings.data_dir / "photos" / photo_id
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "").suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".bin"
    original = directory / ("original" + suffix)
    size = 0
    try:
        with original.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(422, "Фотография должна быть не больше 30 МБ")
                output.write(chunk)
        async with request.app.state.image_semaphore:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "spotly.image_worker",
                str(original),
                str(directory),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            except TimeoutError, asyncio.CancelledError:
                proc.kill()
                await proc.wait()
                raise HTTPException(503, "Обработка фотографии заняла слишком много времени")
        try:
            info = json.loads(stdout)
        except ValueError, TypeError:
            raise HTTPException(
                503, "Не удалось обработать фотографию. Попробуйте ещё раз"
            ) from None
        if proc.returncode:
            raise HTTPException(422, info.get("error", "Не удалось прочитать фотографию"))
        async with request.app.state.sessions() as session, session.begin():
            inv, attempt = await lock_form(session, key)
            existing = await session.scalar(
                select(Photo).where(Photo.invitation_key == key, Photo.upload_id == upload_id)
            )
            if existing:
                return await state_of(session, inv, attempt)
            if attempt.id != attempt_id or attempt.status != "active":
                return await conflict(session, inv, attempt)
            photos = await count_active(session, attempt_id)
            if len(photos) >= 5:
                raise HTTPException(422, "Можно добавить не больше пяти фотографий")
            root = request.app.state.settings.data_dir
            session.add(
                Photo(
                    id=photo_id,
                    invitation_key=key,
                    attempt_id=attempt_id,
                    upload_id=upload_id,
                    name=Path(file.filename or "Фото").name[:255],
                    original=str(original.relative_to(root)),
                    preview=str((directory / "preview.webp").relative_to(root)),
                    thumbnail=str((directory / "thumbnail.webp").relative_to(root)),
                    size=size,
                    position=max((p.position for p in photos), default=-1) + 1,
                    **info,
                )
            )
            mark_changed(inv, attempt, session_id)
            record(
                session,
                key,
                attempt_id,
                "photo_uploaded",
                session_id,
                details={
                    "photo_id": photo_id,
                    "size": size,
                    "width": info["width"],
                    "height": info["height"],
                },
            )
            await session.flush()
            return await state_of(session, inv, attempt)
    finally:
        await file.close()


@router.put("/{key}/photos/order")
async def reorder(key: str, body: Order, request: Request):
    check_key(key)
    async with request.app.state.sessions() as session, session.begin():
        inv, attempt = await lock_form(session, key)
        if stale(inv, attempt, body):
            return await conflict(session, inv, attempt)
        photos = await count_active(session, attempt.id)
        if len(set(body.photo_ids)) != len(photos) or set(body.photo_ids) != {p.id for p in photos}:
            raise HTTPException(422, "Обновите список фотографий")
        for p in photos:
            p.position = body.photo_ids.index(p.id)
        mark_changed(inv, attempt, body.session_id)
        record(session, key, attempt.id, "photos_reordered", body.session_id, value=body.photo_ids)
        await session.flush()
        return await state_of(session, inv, attempt)


@router.delete("/{key}/photos/{photo_id}")
async def remove(key: str, photo_id: str, body: Change, request: Request):
    check_key(key)
    async with request.app.state.sessions() as session, session.begin():
        inv, attempt = await lock_form(session, key)
        if stale(inv, attempt, body):
            return await conflict(session, inv, attempt)
        photo = await session.get(Photo, photo_id)
        if not photo or photo.attempt_id != attempt.id:
            raise HTTPException(404, "Фотография не найдена")
        photo.active = False
        mark_changed(inv, attempt, body.session_id)
        record(
            session,
            key,
            attempt.id,
            "photo_removed",
            body.session_id,
            details={"photo_id": photo_id},
        )
        await session.flush()
        return await state_of(session, inv, attempt)


@router.get("/{key}/photos/{photo_id}/{variant}")
async def get_photo(key: str, photo_id: str, variant: str, request: Request):
    if variant not in {"preview", "thumbnail", "original"}:
        raise HTTPException(404)
    async with request.app.state.sessions() as session:
        photo = await session.get(Photo, photo_id)
        if not photo or photo.invitation_key != key:
            raise HTTPException(404)
        path = request.app.state.settings.data_dir / getattr(photo, variant)
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(
            path,
            media_type=photo.media_type if variant == "original" else "image/webp",
            headers={"Cache-Control": "private, max-age=3600"},
        )
