import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import event
from sqlalchemy.orm import Session
from test_api import opened, payload

from spotly.models import PhotoJob


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


def job_status(client, key, job_id):
    return client.get(f"/api/forms/{key}/photo-jobs/{job_id}")


def worker_command(*args):
    return [sys.executable, "-m", "spotly.photo_jobs_worker", "--once", *args]


def worker_env(client):
    settings = client.app.state.settings
    return os.environ | {"DATABASE_URL": settings.database_url, "DATA_DIR": str(settings.data_dir)}


def run_worker(client, *args, exit_code=0):
    run = subprocess.run(
        worker_command(*args), env=worker_env(client), capture_output=True, text=True, timeout=15
    )
    assert run.returncode == exit_code, run.stderr
    return run.stderr


def test_upload_retry_order_delete_and_isolation(client):
    key, state = opened(client)
    token = str(uuid4())
    first = upload(client, key, state, token)
    assert first.status_code == 202
    job = first.json()
    job_id = job["job_id"]
    assert job["status"] == "queued"
    assert job_status(client, key, job_id).json()["attempt_count"] == 0
    retry = upload(client, key, state, token)
    assert retry.status_code == 202
    assert retry.json()["job_id"] == job_id
    other, _ = opened(client)
    assert job_status(client, other, job_id).status_code == 404

    run_worker(client)
    result = job_status(client, key, job_id).json()
    assert result["status"] == "done"
    assert result["attempt_count"] == result["process_count"] == 1
    assert result["photo_id"] == job_id
    assert upload(client, key, state, token).json()["job_id"] == job_id

    assert upload(client, key, state).status_code == 202
    run_worker(client)
    second = client.get(f"/api/forms/{key}").json()
    ids = [p["id"] for p in second["photos"]]
    assert len(ids) == 2
    reordered = client.put(
        f"/api/forms/{key}/photos/order", json=payload(second, {}) | {"photo_ids": ids[::-1]}
    )
    assert reordered.status_code == 200
    state = reordered.json()
    assert state["photos"][0]["id"] == ids[1]
    assert client.get(f"/api/forms/{other}/photos/{ids[0]}/preview").status_code == 404
    assert client.get(state["photos"][0]["preview"]).headers["content-type"] == "image/webp"
    removed = client.request("DELETE", f"/api/forms/{key}/photos/{ids[1]}", json=payload(state, {}))
    assert removed.status_code == 200
    assert len(removed.json()["photos"]) == 1


def test_failed_job_releases_slot_and_removes_corrupt_original(client, tmp_path):
    key, state = opened(client)
    state = client.put(
        f"/api/forms/{key}/draft", json=payload(state, {"title": "Не потерять"})
    ).json()
    broken = upload(client, key, state, name="broken.jpg", content=b"broken")
    assert broken.status_code == 202
    run_worker(client)
    failure = job_status(client, key, broken.json()["job_id"]).json()
    assert failure["status"] == "failed"
    assert failure["error"]
    assert failure["process_count"] == 0
    restored = client.get(f"/api/forms/{key}").json()
    assert restored["last_photo_error"] == {
        "job_id": broken.json()["job_id"],
        "name": "broken.jpg",
        "error": failure["error"],
    }
    assert list((tmp_path / "photos").iterdir()) == []
    for _ in range(5):
        assert upload(client, key, state).status_code == 202
    assert upload(client, key, state).status_code == 422
    restored = client.get(f"/api/forms/{key}").json()
    assert restored["raw"]["title"] == "Не потерять"
    assert len(restored["pending_photos"]) == 5
    assert restored["last_photo_error"] is None
    assert restored["photos"] == []


def test_upload_does_not_claim_ownership_of_other_devices_text(client):
    key, state = opened(client)
    client.put(
        f"/api/forms/{key}/draft", json=payload(state, {"title": "other device"}, "session-b")
    )
    assert upload(client, key, state).status_code == 202
    run_worker(client)
    result = client.get(f"/api/forms/{key}").json()
    assert result["last_session"] == "session-b"
    assert result["raw"]["title"] == "other device"


def test_concurrent_retry_keeps_one_job_and_original(client, tmp_path):
    key, state = opened(client)
    token = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(upload, client, key, state, token) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert [response.status_code for response in results] == [202, 202]
    job_id = results[0].json()["job_id"]
    assert results[1].json()["job_id"] == job_id
    assert [path.name for path in (tmp_path / "photos").iterdir()] == [job_id]
    run_worker(client)
    assert len(client.get(f"/api/forms/{key}").json()["photos"]) == 1
    for variant in ("original", "preview", "thumbnail"):
        assert client.get(f"/api/forms/{key}/photos/{job_id}/{variant}").status_code == 200


@pytest.mark.parametrize("commit_event", ["before_commit", "after_commit"])
def test_uncertain_enqueue_commit_reconciles_original(client, tmp_path, commit_event):
    key, state = opened(client)

    def remember_job(session, flush_context, instances):
        if any(isinstance(item, PhotoJob) for item in session.new):
            session.info["photo_enqueue"] = True

    def fail_commit(session):
        if session.info.get("photo_enqueue") or any(
            isinstance(item, PhotoJob) for item in session.new
        ):
            raise RuntimeError("injected commit error")

    event.listen(Session, "before_flush", remember_job)
    event.listen(Session, commit_event, fail_commit)
    try:
        with pytest.raises(RuntimeError, match="injected commit error"):
            upload(client, key, state)
    finally:
        event.remove(Session, "before_flush", remember_job)
        event.remove(Session, commit_event, fail_commit)

    pending = client.get(f"/api/forms/{key}").json()["pending_photos"]
    directories = list((tmp_path / "photos").iterdir())
    if commit_event == "before_commit":
        assert pending == []
        assert directories == []
    else:
        assert len(pending) == 1
        job_id = pending[0]["job_id"]
        assert [path.name for path in directories] == [job_id]
        run_worker(client)
        assert job_status(client, key, job_id).json()["status"] == "done"


def test_pending_job_blocks_submit_and_restart_does_not_attach_it_to_new_attempt(client):
    key, state = opened(client)
    accepted = upload(client, key, state)
    assert accepted.status_code == 202
    job_id = accepted.json()["job_id"]
    response = client.post(f"/api/forms/{key}/submit", json=payload(state, {}))
    assert response.status_code == 409
    assert response.json()["code"] == "photos_pending"
    assert response.json()["state"]["pending_photos"][0]["job_id"] == job_id
    restarted = client.post(f"/api/forms/{key}/restart", json=payload(state, {}))
    assert restarted.status_code == 200
    assert restarted.json()["pending_photos"] == []
    run_worker(client)
    assert job_status(client, key, job_id).json()["status"] == "failed"
    assert client.get(f"/api/forms/{key}").json()["photos"] == []


def test_crashed_worker_recovers_same_job_after_lease(client):
    key, state = opened(client)
    accepted = upload(client, key, state)
    assert accepted.status_code == 202
    job_id = accepted.json()["job_id"]

    stopped = run_worker(client, "--lease-seconds", "1", "--crash-after-claim", exit_code=70)
    assert f"claimed job_id={job_id} attempt=1" in stopped
    in_progress = job_status(client, key, job_id).json()
    assert in_progress["status"] == "processing"
    assert in_progress["process_count"] == 0
    time.sleep(1.1)
    recovered = run_worker(client)
    assert f"claimed job_id={job_id} attempt=2" in recovered
    result = job_status(client, key, job_id).json()
    assert result["status"] == "done"
    assert result["attempt_count"] == 2
    assert result["process_count"] == 1
    assert len(client.get(f"/api/forms/{key}").json()["photos"]) == 1


def test_expired_owner_cannot_overwrite_new_workers_photo(client, tmp_path):
    key, state = opened(client)
    accepted = upload(client, key, state, name="second-input.jpg")
    assert accepted.status_code == 202
    job_id = accepted.json()["job_id"]
    log = tmp_path / "slow-worker.log"
    with (
        log.open("w") as output,
        subprocess.Popen(
            worker_command("--lease-seconds", "1", "--pause-after-claim", "6"),
            stdout=output,
            stderr=subprocess.STDOUT,
            env=worker_env(client),
        ) as slow,
    ):
        try:
            deadline = time.monotonic() + 10
            while f"claimed job_id={job_id} attempt=1" not in log.read_text():
                assert slow.poll() is None and time.monotonic() < deadline, log.read_text()
                time.sleep(0.05)
            time.sleep(1.1)
            fast = run_worker(client)
            assert f"claimed job_id={job_id} attempt=2" in fast
            assert slow.poll() is None
            photo_path = f"/api/forms/{key}/photos/{job_id}/preview"
            original_result = client.get(photo_path)
            assert original_result.status_code == 200
            assert slow.wait(timeout=10) == 0
        finally:
            if slow.poll() is None:
                slow.kill()

    assert f"stale_owner job_id={job_id} attempt=1" in log.read_text()
    result = job_status(client, key, job_id).json()
    assert result["status"] == "done"
    assert result["attempt_count"] == 2
    assert result["process_count"] == 1
    assert client.get(photo_path).content == original_result.content
    assert len(client.get(f"/api/forms/{key}").json()["photos"]) == 1
    assert len(list((tmp_path / "photos" / job_id).glob("*/preview.webp"))) == 1
