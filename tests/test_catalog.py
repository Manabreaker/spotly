import os
from uuid import uuid4

import psycopg
import pytest
from test_api import complete_raw, opened, payload
from test_photos import run_worker, upload


def save_event(client, **fields):
    key, state = opened(client)
    raw = complete_raw()
    raw.update(
        format="offline",
        address="Москва, танцевальная студия",
        registration_url="https://example.org/register",
    )
    raw.update(fields)
    response = client.post(f"/api/forms/{key}/submit", json=payload(state, raw))
    assert response.status_code == 200, response.text
    return key, response.json()["event_id"]


def publication_rows():
    url = os.environ["TEST_DATABASE_URL"].replace("+psycopg", "", 1)
    with psycopg.connect(url) as connection:
        return connection.execute(
            "SELECT event_id, city, direction, cover_photo_id FROM catalog_publications"
        ).fetchall()


def test_publication_cli_lifecycle(client, monkeypatch, capsys):
    from spotly.catalog_admin import main

    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    key, event_id = save_event(client, title="Открытый класс")

    assert main(["publish", event_id, "--city", "Москва", "--direction", "Хип-хоп"]) == 0
    assert f"/catalog/{event_id}" in capsys.readouterr().out
    assert main(["publish", event_id, "--city", "Москва", "--direction", "Хаус"]) == 0
    assert publication_rows() == [(event_id, "Москва", "Хаус", None)]
    capsys.readouterr()

    assert main(["list"]) == 0
    listing = capsys.readouterr().out
    assert event_id in listing
    assert "Открытый класс" in listing
    assert key not in listing

    assert main(["unpublish", event_id]) == 0
    assert main(["unpublish", event_id]) == 0
    assert publication_rows() == []


@pytest.mark.parametrize(
    ("link", "options", "unknown_event"),
    [
        ("", ["--city", "Москва", "--direction", "Хаус"], False),
        ("https://example.org/register", ["--city", "Москва", "--direction", " "], False),
        ("https://example.org/register", ["--direction", "Хаус"], False),
        ("https://example.org/register", ["--city", "Москва", "--direction", "Хаус"], True),
    ],
    ids=["missing-link", "missing-direction", "missing-city", "unknown-event"],
)
def test_publication_requires_contact_and_metadata(
    client, monkeypatch, capsys, link, options, unknown_event
):
    from spotly.catalog_admin import main

    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    _, event_id = save_event(client, registration_url=link)
    if unknown_event:
        event_id = str(uuid4())

    assert main(["publish", event_id, *options]) != 0
    assert capsys.readouterr().err
    assert publication_rows() == []


@pytest.fixture
def catalog_admin(monkeypatch):
    from spotly.catalog_admin import main

    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    return main


def test_catalog_filters_and_local_date(client, catalog_admin):
    _, event_id = save_event(client, title="ХАУС 100%", time="00:15")
    _, other_city = save_event(client, title="Хаус: Другой город", address="Тверь, студия")
    _, other_style = save_event(client, title="Хаус без процента")
    _, online = save_event(client, title="Хаус онлайн", format="online")
    _, private_id = save_event(client, title="Прежняя закрытая заявка")

    assert catalog_admin(["publish", event_id, "--city", "Москва", "--direction", "Хаус"]) == 0
    assert catalog_admin(["publish", other_city, "--city", "Тверь", "--direction", "Хаус"]) == 0
    assert catalog_admin(["publish", other_style, "--city", "Москва", "--direction", "Локинг"]) == 0
    assert catalog_admin(["publish", online, "--direction", "Хаус"]) == 0

    filters = {"q": "хаус", "date": "2027-12-01", "city": "Москва", "direction": "Хаус"}
    response = client.get("/catalog", params=filters)
    assert response.status_code == 200
    assert "ХАУС 100%" in response.text
    assert "Хаус онлайн" not in response.text
    assert "Другой город" not in response.text
    assert "Хаус без процента" not in response.text
    home = client.get("/", params=filters)
    assert home.status_code == 200
    assert f'href="/catalog/{event_id}"' in home.text
    assert f'href="/catalog/{other_city}"' not in home.text

    assert "ХАУС 100%" not in client.get("/catalog", params=filters | {"date": "2027-11-30"}).text
    without_date = client.get("/catalog", params=filters | {"date": ""})
    assert without_date.status_code == 200, without_date.text
    assert "ХАУС 100%" in without_date.text

    literal = client.get("/catalog", params={"q": "%"})
    assert "ХАУС 100%" in literal.text
    assert "Хаус без процента" not in literal.text
    assert "Прежняя закрытая заявка" not in client.get("/catalog").text
    assert client.get(f"/catalog/{private_id}").status_code == 404
    assert client.get(f"/catalog/{private_id}/cover").status_code == 404


def test_catalog_filter_uses_the_date_displayed_for_cet_in_summer(client, catalog_admin):
    _, event_id = save_event(
        client, title="Танцы в полночь", timezone="CET", date="2027-07-01", time="00:15"
    )
    assert catalog_admin(["publish", event_id, "--city", "Город", "--direction", "Хаус"]) == 0
    event_path = f"/catalog/{event_id}"
    assert "01.07.2027 · 00:15" in client.get(event_path).text
    assert event_path in client.get("/catalog", params={"date": "2027-07-01"}).text
    assert event_path not in client.get("/catalog", params={"date": "2027-06-30"}).text


def test_catalog_card_and_cover_lifecycle(client, catalog_admin):
    key, state = opened(client)
    uploaded = upload(client, key, state)
    assert uploaded.status_code == 202
    run_worker(client)
    state = client.get(f"/api/forms/{key}").json()
    assert state["photos"][0]["id"] == uploaded.json()["job_id"]
    raw = complete_raw() | {
        "description": "<script>private_markup</script>",
        "join_later": False,
        "join_url": "https://example.org/private-join",
        "registration_url": "https://example.org/register",
    }
    result = client.post(f"/api/forms/{key}/submit", json=payload(state, raw))
    assert result.status_code == 200
    event_id = result.json()["event_id"]
    card_url = f"/catalog/{event_id}"
    cover_url = f"{card_url}/cover"
    assert client.get(card_url).status_code == 404
    assert client.get(cover_url).status_code == 404

    assert catalog_admin(["publish", event_id, "--direction", "Хаус"]) == 0
    assert client.get(card_url).status_code == 200
    assert client.get(cover_url).status_code == 404
    assert catalog_admin(["publish", event_id, "--direction", "Хаус", "--cover"]) == 0

    card = client.get(card_url)
    assert card.status_code == 200
    assert 'href="https://example.org/register"' in card.text
    assert key not in card.text
    assert "https://example.org/private-join" not in card.text
    assert "<script>private_markup</script>" not in card.text
    assert cover_url in card.text
    cover = client.get(cover_url)
    assert cover.status_code == 200
    assert cover.headers["content-type"] == "image/webp"
    assert cover.headers["cache-control"] == "no-store"
    assert cover.content == client.get(state["photos"][0]["preview"]).content

    assert catalog_admin(["unpublish", event_id]) == 0
    assert client.get(card_url).status_code == 404
    assert client.get(cover_url).status_code == 404
    assert client.get(f"/api/forms/{key}").json()["event_id"] == event_id


def test_catalog_entry_and_form_draft(client):
    assert 'href="/catalog"' in client.get("/").text
    assert "Сбросить фильтры" in client.get("/catalog", params={"q": "поиск"}).text

    key, state = opened(client)
    assert client.get(f"/create/{key}").status_code == 200
    draft = {"title": "Личный черновик"}
    saved = client.put(f"/api/forms/{key}/draft", json=payload(state, draft))
    assert saved.status_code == 200
    assert client.get(f"/api/forms/{key}").json()["raw"] == draft
    assert "Личный черновик" not in client.get("/catalog").text
