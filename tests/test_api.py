from uuid import uuid4


def opened(client, key=None):
    key = key or str(uuid4())
    res = client.get(f"/api/forms/{key}")
    assert res.status_code == 200
    return key, res.json()


def payload(state, raw, session="session-a"):
    return dict(
        attempt_id=state["attempt_id"], version=state["version"], raw=raw, session_id=session
    )


def complete_raw():
    return dict(
        title="API проверка",
        description="Создано автоматической проверкой",
        date="2027-12-01",
        time="19:30",
        timezone="Europe/Moscow",
        hours="1",
        minutes="30",
        format="online",
        join_later=True,
        participation="free",
        capacity_mode="unlimited",
    )


def test_invalid_draft_is_saved_and_restored(client):
    key, state = opened(client)
    res = client.put(
        f"/api/forms/{key}/draft", json=payload(state, {"title": "черновик", "date": "2020-01-01"})
    )
    assert res.status_code == 200
    restored = client.get(f"/api/forms/{key}").json()
    assert restored["raw"]["date"] == "2020-01-01"
    assert restored["attempt_id"] == state["attempt_id"]
    assert restored["version"] > state["version"]


def test_stale_tab_cannot_overwrite_and_restart_preserves_history(client):
    key, state = opened(client)
    saved = client.put(f"/api/forms/{key}/draft", json=payload(state, {"title": "old"})).json()
    conflict = client.put(
        f"/api/forms/{key}/draft", json=payload(state, {"title": "stale"}, "session-b")
    )
    assert conflict.status_code == 409
    assert conflict.json()["state"]["raw"]["title"] == "old"
    restarted = client.post(f"/api/forms/{key}/restart", json=payload(saved, {})).json()
    assert restarted["attempt_id"] != saved["attempt_id"]
    assert restarted["raw"] == {}
    assert (
        client.put(
            f"/api/forms/{key}/draft", json=payload(saved, {"title": "overwrite"})
        ).status_code
        == 409
    )


def test_submit_validates_then_is_idempotent(client):
    key, state = opened(client)
    rejected = client.post(f"/api/forms/{key}/submit", json=payload(state, {"title": "incomplete"}))
    assert rejected.status_code == 422
    assert "description" in rejected.json()["errors"]
    state = rejected.json()["state"]
    sent = payload(state, complete_raw())
    first = client.post(f"/api/forms/{key}/submit", json=sent)
    assert first.status_code == 200
    second = client.post(f"/api/forms/{key}/submit", json=sent)
    assert second.status_code == 200
    assert first.json()["event_id"] == second.json()["event_id"]
    assert client.get(f"/api/forms/{key}").json()["status"] == "completed"
    assert client.post(f"/api/forms/{key}/restart", json=sent).status_code == 409


def test_events_deduplicate_and_reject_other_attempt(client):
    key, state = opened(client)
    other_key, other = opened(client)
    event = dict(
        id=str(uuid4()),
        attempt_id=state["attempt_id"],
        session_id=str(uuid4()),
        sequence=1,
        name="field_error",
        field="date",
        value="2020-01-01",
        occurred_at="2026-10-15T12:00:00Z",
        details={"code": "past"},
    )
    assert client.post(f"/api/forms/{key}/events", json={"events": [event]}).json()["accepted"] == 1
    assert client.post(f"/api/forms/{key}/events", json={"events": [event]}).json()["accepted"] == 0
    event["id"] = str(uuid4())
    event["attempt_id"] = other["attempt_id"]
    assert client.post(f"/api/forms/{key}/events", json={"events": [event]}).status_code == 403


def test_feedback_is_optional_and_score_bounded(client):
    key, state = opened(client)
    body = dict(attempt_id=state["attempt_id"], kind="difficulty", comment="Не понял дату")
    assert client.post(f"/api/forms/{key}/feedback", json=body).status_code == 200
    final = client.post(f"/api/forms/{key}/submit", json=payload(state, complete_raw())).json()
    body = dict(attempt_id=state["attempt_id"], kind="final", comment="", score=10)
    assert client.post(f"/api/forms/{key}/feedback", json=body).status_code == 200
    assert client.post(f"/api/forms/{key}/feedback", json=body | {"score": 11}).status_code == 422
    assert final["event_id"]


def test_html_never_renders_user_content_as_markup(client):
    key, state = opened(client)
    client.put(
        f"/api/forms/{key}/draft", json=payload(state, {"title": '<script>alert("x")</script>'})
    )
    html = client.get(f"/create/{key}")
    assert html.status_code == 200
    assert '<script>alert("x")</script>' not in html.text


def test_repeated_difficulty_feedback_is_preserved(client):
    import asyncio

    from sqlalchemy import select

    from spotly.models import Feedback

    key, state = opened(client)
    for message in ["Первый вопрос", "Второй вопрос"]:
        response = client.post(
            f"/api/forms/{key}/feedback",
            json={
                "id": str(uuid4()),
                "attempt_id": state["attempt_id"],
                "kind": "difficulty",
                "comment": message,
            },
        )
        assert response.status_code == 200

    async def comments():
        async with client.app.state.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(Feedback.comment).where(Feedback.invitation_key == key)
                    )
                ).all()
            )

    assert sorted(asyncio.run(comments())) == ["Второй вопрос", "Первый вопрос"]


def test_restart_preserves_latest_local_values(client):
    import asyncio

    from spotly.models import Attempt

    key, state = opened(client)
    result = client.post(
        f"/api/forms/{key}/restart", json=payload(state, {"title": "Не успело сохраниться"})
    )
    assert result.status_code == 200

    async def old_raw():
        async with client.app.state.sessions() as session:
            return (await session.get(Attempt, state["attempt_id"])).raw

    assert asyncio.run(old_raw())["title"] == "Не успело сохраниться"


def test_concurrent_submit_creates_exactly_one_event(client):
    from concurrent.futures import ThreadPoolExecutor

    key, state = opened(client)
    sent = payload(state, complete_raw())

    def send(_):
        response = client.post(f"/api/forms/{key}/submit", json=sent)
        assert response.status_code == 200
        return response.json()["event_id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(send, range(4)))
    assert len(set(ids)) == 1


def test_research_identifies_participant_without_copying_access_key(client):
    key, state = opened(client)
    assert state["participant_id"] != key
    assert len(state["participant_id"]) == 64
