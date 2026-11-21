import argparse
import asyncio
import fcntl
import json
import logging
import math
import os
import shutil
import socket
import sys
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from .config import Settings
from .db import database
from .models import Photo, PhotoJob
from .service import lock_form, mark_changed, record

log = logging.getLogger(__name__)
CONVERSION_TIMEOUT = 60
CONVERSION_ERROR = "Не удалось обработать фотографию. Попробуйте ещё раз"


async def claim_job(sessions, lease_seconds):
    candidate = (
        select(PhotoJob.id)
        .where(
            or_(
                PhotoJob.status == "queued",
                and_(
                    PhotoJob.status == "processing",
                    PhotoJob.lease_until <= func.clock_timestamp(),
                ),
            )
        )
        .order_by(PhotoJob.created_at, PhotoJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    async with sessions() as session, session.begin():
        job = await session.scalar(
            update(PhotoJob)
            .where(PhotoJob.id == candidate)
            .values(
                status="processing",
                claim_token=str(uuid4()),
                lease_until=func.clock_timestamp() + timedelta(seconds=lease_seconds),
                attempt_count=PhotoJob.attempt_count + 1,
            )
            .returning(PhotoJob)
        )
    return job


async def convert_photo(original, target):
    if not original.is_file():
        return {}, "Оригинал фотографии недоступен"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "spotly.image_worker",
        str(original),
        str(target),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=CONVERSION_TIMEOUT)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return {}, "Обработка фотографии заняла слишком много времени"

    try:
        info = json.loads(stdout)
    except ValueError:
        return {}, CONVERSION_ERROR
    if proc.returncode:
        return {}, info.get("error") or CONVERSION_ERROR
    return info, None


async def finish_job(sessions, job, directory, info, error):
    async with sessions() as session, session.begin():
        inv, attempt = await lock_form(session, job.invitation_key)
        if inv.attempt_id != job.attempt_id or attempt.status != "active":
            error = "Черновик изменился. Загрузите фотографию в текущей форме"

        status = "failed" if error else "done"
        updated = await session.scalar(
            update(PhotoJob)
            .where(
                PhotoJob.id == job.id,
                PhotoJob.status == "processing",
                PhotoJob.claim_token == job.claim_token,
                PhotoJob.lease_until > func.clock_timestamp(),
            )
            .values(
                status=status,
                claim_token=None,
                lease_until=None,
                process_count=PhotoJob.process_count + int(status == "done"),
                error=error,
            )
            .returning(PhotoJob.id)
        )
        if updated is None:
            return "stale_owner"
        if status == "failed":
            return status

        last_position = await session.scalar(
            select(func.max(Photo.position)).where(Photo.attempt_id == attempt.id, Photo.active)
        )
        session.add(
            Photo(
                id=job.id,
                invitation_key=inv.key,
                attempt_id=attempt.id,
                upload_id=job.upload_id,
                name=job.name,
                original=job.original,
                preview=str(directory / "preview.webp"),
                thumbnail=str(directory / "thumbnail.webp"),
                size=job.size,
                position=0 if last_position is None else last_position + 1,
                **info,
            )
        )
        mark_changed(inv, attempt, job.session_id, text_change=False)
        record(
            session,
            inv.key,
            attempt.id,
            "photo_uploaded",
            job.session_id,
            details={"photo_id": job.id, "size": job.size, **info},
        )
    return status


def remove_outputs(path):
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("photo_cleanup_failed job_dir=%s", path.name)


async def process_one(sessions, root, args):
    job = await claim_job(sessions, args.lease_seconds)
    if job is None:
        return False

    log.info(
        "claimed job_id=%s attempt=%s claim_token=%s", job.id, job.attempt_count, job.claim_token
    )
    if args.crash_after_claim:
        log.info("crash_after_claim job_id=%s", job.id)
        os._exit(70)
    if args.pause_after_claim:
        await asyncio.sleep(args.pause_after_claim)

    original = root / job.original
    directory = original.parent / job.claim_token
    info, error = await convert_photo(original, directory)
    # Backup reads the DB and then archives paths from this directory.
    photo_directory_fd = os.open(root / "photos", os.O_RDONLY)
    try:
        fcntl.flock(photo_directory_fd, fcntl.LOCK_SH)
        status = await finish_job(sessions, job, directory.relative_to(root), info, error)
        log.info(
            "%s job_id=%s attempt=%s claim_token=%s",
            status,
            job.id,
            job.attempt_count,
            job.claim_token,
        )
        if status == "stale_owner":
            remove_outputs(directory)
        elif status == "failed":
            remove_outputs(original.parent)
    finally:
        os.close(photo_directory_fd)
    return True


async def run(args):
    settings = Settings()
    (settings.data_dir / "photos").mkdir(parents=True, exist_ok=True)
    engine, sessions = database(settings.database_url)
    try:
        while True:
            try:
                processed = await process_one(sessions, settings.data_dir, args)
            except SQLAlchemyError:
                if args.once:
                    raise
                log.exception("photo_worker_database_error")
                processed = False
            if args.once:
                if not processed:
                    log.info("no_jobs")
                return
            if not processed:
                await asyncio.sleep(1)
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="Process Spotly photo jobs")
    parser.add_argument("--once", action="store_true", help="process at most one eligible job")
    parser.add_argument("--lease-seconds", type=float, default=90, help="claim lifetime in seconds")
    parser.add_argument(
        "--crash-after-claim", action="store_true", help="exit after a committed claim"
    )
    parser.add_argument(
        "--pause-after-claim", type=float, default=0, help="pause before conversion"
    )
    args = parser.parse_args()
    if not math.isfinite(args.lease_seconds) or args.lease_seconds <= 0:
        parser.error("--lease-seconds must be positive and finite")
    if not math.isfinite(args.pause_after_claim) or args.pause_after_claim < 0:
        parser.error("--pause-after-claim must be non-negative and finite")
    worker = socket.gethostname()
    logging.basicConfig(
        level=logging.INFO, format=f"%(asctime)s worker={worker}:%(process)d %(message)s"
    )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
