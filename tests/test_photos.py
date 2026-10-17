import os
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Event
from uuid import uuid4

import psycopg
import pytest
from PIL import Image
from sqlalchemy import event
from sqlalchemy.orm import Session
from test_api import opened, payload

from spotly.models import Photo


def upload(client, key, state, upload_id=None, name="photo.jpg", content=None):
    if content is None:
        stream = BytesIO()
        Image.new("RGB", (30, 20), "red").save(stream, "JPEG")
        content = stream.getvalue()
    return client.post(
        f"/api/forms/{key}/photos",
        data={
            "attempt_id": state["attempt_id"],
            "session_id": "session-a",
            "upload_id": upload_id or str(uuid4()),
        },
        files={"file": (name, content, "application/octet-stream")},
    )


def test_upload_retry_order_delete_and_isolation(client):
    key, state = opened(client)
    token = str(uuid4())
    first = upload(client, key, state, token)
    assert first.status_code == 200
    retry = upload(client, key, state, token)
    assert retry.status_code == 200
    assert len(retry.json()["photos"]) == 1
    second = upload(client, key, state).json()
    ids = [p["id"] for p in second["photos"]]
    reordered = client.put(
        f"/api/forms/{key}/photos/order", json=payload(second, {}) | {"photo_ids": ids[::-1]}
    )
    assert reordered.status_code == 200
    state = reordered.json()
    assert state["photos"][0]["id"] == ids[1]
    other, _ = opened(client)
    assert client.get(f"/api/forms/{other}/photos/{ids[0]}/preview").status_code == 404
    assert client.get(state["photos"][0]["preview"]).headers["content-type"] == "image/webp"
    removed = client.request("DELETE", f"/api/forms/{key}/photos/{ids[1]}", json=payload(state, {}))
    assert removed.status_code == 200
    assert len(removed.json()["photos"]) == 1


def test_five_photo_limit_and_corrupt_upload_preserve_draft(client):
    key, state = opened(client)
    state = client.put(
        f"/api/forms/{key}/draft", json=payload(state, {"title": "Не потерять"})
    ).json()
    assert upload(client, key, state, name="broken.jpg", content=b"broken").status_code == 422
    for _ in range(5):
        res = upload(client, key, state)
        assert res.status_code == 200
    assert upload(client, key, state).status_code == 422
    restored = client.get(f"/api/forms/{key}").json()
    assert restored["raw"]["title"] == "Не потерять"
    assert len(restored["photos"]) == 5


def test_upload_does_not_claim_ownership_of_other_devices_text(client):
    key, state = opened(client)
    client.put(
        f"/api/forms/{key}/draft", json=payload(state, {"title": "other device"}, "session-b")
    )
    result = upload(client, key, state).json()
    assert result["last_session"] == "session-b"
    assert result["raw"]["title"] == "other device"


def test_concurrent_retry_keeps_one_photo_directory(client, tmp_path):
    key, state = opened(client)
    token = str(uuid4())
    semaphore = client.app.state.image_semaphore
    client.portal.call(semaphore.acquire)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(upload, client, key, state, token) for _ in range(2)]
        try:
            deadline = time.monotonic() + 10
            while len(list(tmp_path.glob("photos/*/original.jpg"))) < 2:
                assert time.monotonic() < deadline, "both uploads must reach image processing"
                time.sleep(0.01)
        finally:
            client.portal.call(semaphore.release)
        results = [future.result(timeout=20) for future in futures]
    assert [response.status_code for response in results] == [200, 200]
    photo_id = results[0].json()["photos"][0]["id"]
    assert results[1].json()["photos"][0]["id"] == photo_id
    assert len(client.get(f"/api/forms/{key}").json()["photos"]) == 1
    assert [directory.name for directory in (tmp_path / "photos").iterdir()] == [photo_id]
    assert sorted(path.name for path in (tmp_path / "photos" / photo_id).iterdir()) == [
        "original.jpg",
        "preview.webp",
        "thumbnail.webp",
    ]
    for variant in ("original", "preview", "thumbnail"):
        assert client.get(f"/api/forms/{key}/photos/{photo_id}/{variant}").status_code == 200


def test_corrupt_upload_leaves_no_files(client, tmp_path):
    key, state = opened(client)
    response = upload(client, key, state, content=b"broken image")
    assert response.status_code == 422
    assert list((tmp_path / "photos").iterdir()) == []


@pytest.mark.parametrize("commit_event", ["before_commit", "after_commit"])
def test_commit_error_reconciles_photo_files_with_database(client, tmp_path, commit_event):
    key, state = opened(client)

    def remember_photo(session, flush_context, instances):
        if any(isinstance(item, Photo) for item in session.new):
            session.info["photo_upload"] = True

    def fail_commit(session):
        if session.info.get("photo_upload"):
            raise RuntimeError("injected commit error")

    event.listen(Session, "before_flush", remember_photo)
    event.listen(Session, commit_event, fail_commit)
    try:
        with pytest.raises(RuntimeError, match="injected commit error"):
            upload(client, key, state)
    finally:
        event.remove(Session, "before_flush", remember_photo)
        event.remove(Session, commit_event, fail_commit)

    photos = client.get(f"/api/forms/{key}").json()["photos"]
    directories = list((tmp_path / "photos").iterdir())
    if commit_event == "before_commit":
        assert photos == []
        assert directories == []
    else:
        assert len(photos) == 1
        assert [directory.name for directory in directories] == [photos[0]["id"]]
        for variant in ("original", "preview", "thumbnail"):
            assert (
                client.get(f"/api/forms/{key}/photos/{photos[0]['id']}/{variant}").status_code
                == 200
            )


def test_cleanup_waits_for_inflight_form_transaction(client, tmp_path):
    key, state = opened(client)
    semaphore = client.app.state.image_semaphore
    client.portal.call(semaphore.acquire)
    cleanup_started = Event()

    def checked_out(dbapi_connection, connection_record, connection_proxy):
        cleanup_started.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(upload, client, key, state, content=b"broken image")
        deadline = time.monotonic() + 10
        try:
            while not list(tmp_path.glob("photos/*/original.jpg")):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            url = os.environ["TEST_DATABASE_URL"].replace("postgresql+psycopg:", "postgresql:")
            with psycopg.connect(url, connect_timeout=5) as connection:
                connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
                event.listen(client.app.state.engine.sync_engine, "checkout", checked_out)
                client.portal.call(semaphore.release)
                assert cleanup_started.wait(10)
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.5)
                assert len(list(tmp_path.glob("photos/*/original.jpg"))) == 1
        finally:
            if semaphore.locked():
                client.portal.call(semaphore.release)
            if event.contains(client.app.state.engine.sync_engine, "checkout", checked_out):
                event.remove(client.app.state.engine.sync_engine, "checkout", checked_out)
        assert future.result(timeout=10).status_code == 422
    assert list((tmp_path / "photos").iterdir()) == []
