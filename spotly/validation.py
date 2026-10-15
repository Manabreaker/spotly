import re
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LIMITS = {
    "title": 150,
    "description": 10000,
    "address": 500,
    "join_url": 4096,
    "registration_url": 4096,
}


def normalize_url(value):
    value = str(value or "").strip()
    if not value:
        return None
    if len(value) > 4096 or re.search(r"[\s\\<>]", value):
        raise ValueError("Укажите корректную ссылку длиной до 4096 символов")
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    if (
        parts.scheme.lower() not in {"https", "http"}
        or not parts.hostname
        or parts.username
        or parts.password
        or "." not in parts.hostname
    ):
        raise ValueError("Укажите веб-ссылку, например example.ru/meeting")
    parts.hostname.encode("idna")
    _ = parts.port
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, parts.fragment))


def validate_event(raw, now=None):
    now = now or datetime.now(UTC)
    errors, data = {}, {}
    for field in ("title", "description"):
        data[field] = str(raw.get(field) or "").strip()
        if not data[field]:
            errors[field] = "Заполните это поле"
        elif len(data[field]) > LIMITS[field]:
            errors[field] = f"Не больше {LIMITS[field]} символов"
    zone = None
    try:
        zone = ZoneInfo(str(raw.get("timezone", "")))
        data["timezone"] = zone.key
    except ZoneInfoNotFoundError, ValueError:
        errors["timezone"] = "Выберите часовой пояс"
    if zone:
        try:
            naive = datetime.fromisoformat(f"{raw.get('date', '')}T{raw.get('time', '')}")
            if naive.tzinfo is not None:
                raise ValueError("local time required")
            local = naive.replace(tzinfo=zone)
            utc = local.astimezone(UTC)
            if utc.astimezone(zone).replace(tzinfo=None) != naive:
                errors["time"] = "Такого времени нет из-за перевода часов. Выберите другое"
            elif local.utcoffset() != naive.replace(tzinfo=zone, fold=1).utcoffset():
                errors["time"] = "Это время повторяется при переводе часов. Выберите другое"
            elif utc <= now:
                errors["date"] = "Начало мероприятия должно быть в будущем"
            data["starts_at"] = utc.isoformat()
        except ValueError, TypeError, OverflowError:
            errors["date"] = "Укажите дату и время начала"
    try:
        hours, minutes = str(raw.get("hours") or "0"), str(raw.get("minutes") or "0")
        if not hours.isdecimal() or not minutes.isdecimal():
            raise ValueError()
        duration = int(hours) * 60 + int(minutes)
        if int(minutes) > 59 or not 1 <= duration <= 43200:
            raise ValueError()
        data["duration_minutes"] = duration
    except ValueError, OverflowError:
        errors["duration"] = "Укажите длительность от 1 минуты до 30 суток; минуты — от 0 до 59"
    data["format"] = raw.get("format")
    data["address"], data["join_url"], data["join_later"] = None, None, False
    if data["format"] == "offline":
        address = str(raw.get("address") or "").strip()
        if not address or len(address) > 500:
            errors["address"] = "Укажите адрес длиной до 500 символов"
        data["address"] = address
    elif data["format"] == "online":
        data["join_later"] = raw.get("join_later") is True
        if not data["join_later"]:
            try:
                data["join_url"] = normalize_url(raw.get("join_url"))
                if not data["join_url"]:
                    raise ValueError("Укажите ссылку или выберите «Ссылку укажу позже»")
            except (ValueError, UnicodeError) as exc:
                errors["join_url"] = str(exc)
    else:
        errors["format"] = "Выберите формат мероприятия"
    data["price_kopecks"] = 0
    if raw.get("participation") == "paid":
        price = str(raw.get("price") or "").strip().replace(",", ".")
        if not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,2})?", price):
            errors["price"] = "Укажите сумму до 9 999 999.99 ₽, не больше двух знаков после точки"
        else:
            amount = int(Decimal(price) * 100)
            if not 1 <= amount <= 999999999:
                errors["price"] = "Для платного участия укажите стоимость больше нуля"
            data["price_kopecks"] = amount
    elif raw.get("participation") != "free":
        errors["participation"] = "Выберите, платное ли участие"
    data["capacity"] = None
    if raw.get("capacity_mode") == "limited":
        value = str(raw.get("capacity") or "")
        if not re.fullmatch(r"[0-9]{1,7}", value) or not 1 <= int(value) <= 1000000:
            errors["capacity"] = "Укажите целое число мест от 1 до 1 000 000"
        else:
            data["capacity"] = int(value)
    elif raw.get("capacity_mode") != "unlimited":
        errors["capacity_mode"] = "Укажите, ограничено ли количество мест"
    try:
        data["registration_url"] = normalize_url(raw.get("registration_url"))
    except (ValueError, UnicodeError) as exc:
        errors["registration_url"] = str(exc)
    return data, errors
