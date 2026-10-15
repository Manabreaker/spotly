from io import BytesIO
from uuid import uuid4

from PIL import Image
from test_api import opened, payload


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
