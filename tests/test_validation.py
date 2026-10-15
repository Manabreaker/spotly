from datetime import UTC, datetime

import pytest

from spotly.validation import validate_event

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def valid():
    return dict(
        title="Встреча",
        description="Описание",
        date="2026-12-01",
        time="19:30",
        timezone="Asia/Yekaterinburg",
        hours="1",
        minutes="30",
        format="online",
        join_later=True,
        participation="free",
        capacity_mode="unlimited",
    )


def test_normalizes_event_time_and_optional_fields():
    data, errors = validate_event(valid(), NOW)
    assert errors == {}
    assert data["starts_at"] == "2026-12-01T14:30:00+00:00"
    assert data["duration_minutes"] == 90
    assert data["price_kopecks"] == 0
    assert data["capacity"] is None
    assert data["join_url"] is None


def test_normalizes_comma_money_and_schemeless_urls():
    raw = valid() | dict(
        participation="paid",
        price="1500,50",
        join_later=False,
        join_url="zoom.us/j/123?pwd=visible",
        registration_url="example.org/register",
    )
    data, errors = validate_event(raw, NOW)
    assert errors == {}
    assert data["price_kopecks"] == 150050
    assert data["join_url"] == "https://zoom.us/j/123?pwd=visible"
    assert data["registration_url"] == "https://example.org/register"


@pytest.mark.parametrize(
    ("field", "value", "error_field"),
    [
        ("title", " ", "title"),
        ("title", "x" * 151, "title"),
        ("description", "x" * 10001, "description"),
        ("date", "2020-01-01", "date"),
        ("timezone", "invalid/zone", "timezone"),
        ("hours", "721", "duration"),
        ("minutes", "60", "duration"),
        ("hours", "-1", "duration"),
        ("format", "", "format"),
        ("participation", "", "participation"),
        ("capacity_mode", "", "capacity_mode"),
    ],
)
def test_rejects_invalid_values(field, value, error_field):
    _, errors = validate_event(valid() | {field: value}, NOW)
    assert error_field in errors


@pytest.mark.parametrize("price", ["0", "-1", "NaN", "Infinity", "1e3", "1.234", "10000000"])
def test_rejects_invalid_paid_amounts(price):
    _, errors = validate_event(valid() | dict(participation="paid", price=price), NOW)
    assert "price" in errors


def test_rejects_zero_duration_and_capacity_and_dangerous_url():
    _, errors = validate_event(
        valid()
        | dict(
            hours="0",
            minutes="0",
            capacity_mode="limited",
            capacity="0",
            registration_url="javascript:alert(1)",
        ),
        NOW,
    )
    assert {"duration", "capacity", "registration_url"} <= errors.keys()


def test_offline_ignores_hidden_online_url():
    data, errors = validate_event(
        valid() | dict(format="offline", address="Москва, Лесная, 1", join_url="garbage"), NOW
    )
    assert errors == {}
    assert data["address"] == "Москва, Лесная, 1"
    assert data["join_url"] is None


def test_ambiguous_and_nonexistent_local_times_are_not_silently_shifted():
    for date in ["2027-03-14", "2026-11-01"]:
        _, errors = validate_event(
            valid()
            | dict(
                date=date, time="02:30" if "03" in date else "01:30", timezone="America/New_York"
            ),
            NOW,
        )
        assert "time" in errors


def test_boundary_amount_capacity_and_duration_are_accepted():
    data, errors = validate_event(
        valid()
        | dict(
            participation="paid",
            price="9999999.99",
            capacity_mode="limited",
            capacity="1000000",
            hours="720",
            minutes="0",
        ),
        NOW,
    )
    assert errors == {}
    assert (data["price_kopecks"], data["capacity"], data["duration_minutes"]) == (
        999999999,
        1000000,
        43200,
    )
