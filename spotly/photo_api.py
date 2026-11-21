import re
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field
from sqlalchemy import select, text

from .api import Change, check_key, conflict
from .models import PENDING_PHOTO_JOBS, Photo, PhotoJob
from .service import lock_form, mark_changed, pending_photo_jobs, record, stale, state_of

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


def photo_result(key, photo_id):
    return {"photo_id": photo_id, "preview": f"/api/forms/{key}/photos/{photo_id}/preview"}


def job_view(job):
    result = {
        "job_id": job.id,
        "name": job.name,
        "status": job.status,
        "attempt_count": job.attempt_count,
        "process_count": job.process_count,
        "error": job.error,
    }
    if job.status == "done":
        result.update(photo_result(job.invitation_key, job.id))
    return result


def existing_photo_view(photo):
    return {
        "job_id": photo.id,
        "name": photo.name,
        "status": "done",
        **photo_result(photo.invitation_key, photo.id),
    }


async def check_upload(session, key, attempt_id, upload_id):
    inv, attempt = await lock_form(session, key)
    job = await session.scalar(
        select(PhotoJob).where(PhotoJob.invitation_key == key, PhotoJob.upload_id == upload_id)
    )
    if job is not None:
        status_code = 202 if job.status in PENDING_PHOTO_JOBS else 200
        return JSONResponse(job_view(job), status_code=status_code)
    existing = await session.scalar(
        select(Photo).where(Photo.invitation_key == key, Photo.upload_id == upload_id)
    )
    if existing is not None:
        return JSONResponse(existing_photo_view(existing))
    if attempt.id != attempt_id or attempt.status != "active":
        return await conflict(session, inv, attempt)
    ready = await count_active(session, attempt.id)
    pending = await pending_photo_jobs(session, attempt.id)
    if len(ready) + len(pending) >= 5:
        raise HTTPException(422, "Можно добавить не больше пяти фотографий")
    return None


@router.post("/{key}/photos", status_code=202)
async def upload_photo(
    key: str,
    request: Request,
    attempt_id: str = Form(max_length=36),
    session_id: str = Form(max_length=100),
    upload_id: str = Form(max_length=36),
    file: UploadFile = File(),
):
    check_key(key)
    sessions = request.app.state.sessions
    async with sessions() as session, session.begin():
        previous = await check_upload(session, key, attempt_id, upload_id)
        if previous is not None:
            return previous

    job_id = str(uuid4())
    root = request.app.state.settings.data_dir
    directory = root / "photos" / job_id
    directory.mkdir(parents=True)
    suffix = Path(file.filename or "").suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".bin"
    original = directory / ("original" + suffix)
    size = 0
    committed = False
    try:
        with original.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(422, "Фотография должна быть не больше 30 МБ")
                output.write(chunk)

        async with sessions() as session, session.begin():
            previous = await check_upload(session, key, attempt_id, upload_id)
            if previous is not None:
                return previous
            job = PhotoJob(
                id=job_id,
                invitation_key=key,
                attempt_id=attempt_id,
                upload_id=upload_id,
                session_id=session_id,
                name=Path(file.filename or "Фото").name[:255],
                original=str(original.relative_to(root)),
                size=size,
            )
            session.add(job)
        committed = True
        return job_view(job)
    finally:
        try:
            if not committed:
                # Reconcile a commit whose result may have been lost. Only this
                # request's directory is eligible for removal.
                async with sessions() as session, session.begin():
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                        {"key": key},
                    )
                    if await session.get(PhotoJob, job_id) is None:
                        shutil.rmtree(directory)
        finally:
            await file.close()


@router.get("/{key}/photo-jobs/{job_id}")
async def get_photo_job(key: str, job_id: str, request: Request):
    check_key(key)
    async with request.app.state.sessions() as session:
        job = await session.get(PhotoJob, job_id)
        if job is not None and job.invitation_key == key:
            return job_view(job)
        photo = await session.get(Photo, job_id)
        if photo is not None and photo.invitation_key == key:
            return existing_photo_view(photo)
        raise HTTPException(404, "Задание не найдено")


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
        mark_changed(inv, attempt, body.session_id, text_change=False)
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
        mark_changed(inv, attempt, body.session_id, text_change=False)
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
