"""Поиск достопримечательностей через Overpass около заданного центра.

Запрос ограничен радиусом и узким набором тегов OSM. Для интересов
джиу-джитсу и BJJ в тот же радиус добавляются sport=jiu-jitsu,
sport=martial_arts и залы только с таким тегом или явным названием.
После ответа каждый объект повторно проверяется по расстоянию до центра.
В результат попадают только точки с координатами и OSM ID.
CrewAI здесь не используется.
"""

from __future__ import annotations

import json
import math
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Any

from anyascii import anyascii

from .config import OsmSettings, load_osm_settings
from .osm_http import urlopen

# Не чаще одного запроса в секунду к публичному Overpass.
_MIN_REQUEST_INTERVAL_SEC = 1.0

# Ограниченный набор типов достопримечательностей (ключ тега, значение).
ATTRACTION_TAGS: tuple[tuple[str, str], ...] = (
    ("tourism", "museum"),
    ("tourism", "attraction"),
    ("tourism", "viewpoint"),
    ("tourism", "gallery"),
    ("tourism", "zoo"),
    ("historic", "monument"),
    ("historic", "castle"),
    ("historic", "archaeological_site"),
    ("natural", "beach"),
    ("leisure", "nature_reserve"),
)

# Залы ищем только вместе с sport-тегом или явным названием джиу-джитсу/BJJ.
_JIU_JITSU_HALL_TAGS: tuple[tuple[str, str], ...] = (
    ("amenity", "dojo"),
    ("leisure", "fitness_centre"),
    ("leisure", "sports_centre"),
    ("leisure", "sports_hall"),
)
_JIU_JITSU_SPORTS = frozenset({"jiu-jitsu", "martial_arts"})
_JIU_JITSU_NAME_RE = re.compile(
    r"джиу[\s\-–—]*джитсу|jiu[\s\-–—]*jitsu|(?<![0-9a-zа-яё])bjj(?![0-9a-zа-яё])",
    re.IGNORECASE,
)

# Фраза для поручений, если по интересу нет объекта в ответе OSM.
OSM_INTEREST_NOT_FOUND = "не найдено в OpenStreetMap"

_last_request_at = 0.0


class OverpassFailure(str, Enum):
    """Тип сбоя Overpass для UI (без сырого текста исключения)."""

    TIMEOUT = "timeout"
    OVERLOAD = "overload"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    OTHER = "other"


_OVERPASS_USER_MESSAGE = {
    OverpassFailure.TIMEOUT: (
        "Поиск городов в регионе не уложился в отведённое время. "
        "Нажмите «Повторить поиск» позже."
    ),
    OverpassFailure.OVERLOAD: (
        "Сервис карт перегружен. Подождите и нажмите «Повторить поиск»."
    ),
    OverpassFailure.RATE_LIMIT: (
        "Слишком частые запросы к картам. Подождите и нажмите «Повторить поиск»."
    ),
    OverpassFailure.NETWORK: (
        "Нет связи с сервисом карт. Проверьте интернет и нажмите «Повторить поиск»."
    ),
    OverpassFailure.OTHER: (
        "Не удалось загрузить города в этом регионе. Нажмите «Повторить поиск»."
    ),
}


def overpass_user_message(kind: OverpassFailure) -> str:
    """Короткий текст для интерфейса по типу сбоя."""
    return _OVERPASS_USER_MESSAGE[kind]


class PlacesError(Exception):
    """Не удалось получить или разобрать ответ Overpass."""

    def __init__(
        self,
        message: str,
        *,
        kind: OverpassFailure = OverpassFailure.OTHER,
    ) -> None:
        self.message = message
        self.kind = kind
        super().__init__(message)


@dataclass(frozen=True)
class Place:
    osm_type: str
    osm_id: int
    name: str
    latitude: float
    longitude: float
    distance_m: float
    category: str
    tags: tuple[tuple[str, str], ...] = ()


def jiu_jitsu_interests(interests: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Интересы «джиу-джитсу», «бразильское джиу-джитсу» и BJJ."""
    return tuple(item for item in interests if _interest_is_jiu_jitsu(item))


def place_matches_jiu_jitsu(place: Place) -> bool:
    """Объект с sport=jiu-jitsu / martial_arts или зал с таким названием."""
    return _matches_jiu_jitsu(dict(place.tags))


def search_places(
    latitude: float,
    longitude: float,
    radius_m: int | None = None,
    *,
    settings: OsmSettings | None = None,
    interests: tuple[str, ...] | list[str] | None = None,
) -> list[Place]:
    """Найти достопримечательности в радиусе от центра.

    Сначала Overpass ограничивает выборку around-радиусом, затем каждый
    элемент отбрасывается, если расстояние до центра больше radius_m.
    Для интересов джиу-джитсу в тот же радиус добавляются sport=jiu-jitsu,
    sport=martial_arts и залы только с этими тегами или явным названием.
    """
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise PlacesError("Координаты центра должны быть конечными числами.")
    if abs(latitude) > 90 or abs(longitude) > 180:
        raise PlacesError("Координаты центра вне допустимого диапазона.")

    resolved = load_osm_settings() if settings is None else settings
    radius = resolved.search_radius_m if radius_m is None else radius_m
    if type(radius) is not int or radius < 1:
        raise PlacesError("Радиус должен быть целым числом метров не меньше 1.")

    include_jiu_jitsu = bool(jiu_jitsu_interests(interests or ()))
    elements = _fetch_overpass(
        latitude,
        longitude,
        radius,
        resolved,
        include_jiu_jitsu=include_jiu_jitsu,
    )
    places: list[Place] = []
    seen: set[tuple[str, int]] = set()
    for element in elements:
        place = _element_to_place(
            element,
            latitude,
            longitude,
            radius,
            include_jiu_jitsu=include_jiu_jitsu,
        )
        if place is None:
            continue
        key = (place.osm_type, place.osm_id)
        if key in seen:
            continue
        seen.add(key)
        places.append(place)
    places.sort(key=lambda item: (item.distance_m, item.name.casefold()))
    return places


def distance_meters(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Расстояние между двумя точками по формуле гаверсинуса, метры."""
    radius_earth_m = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * radius_earth_m * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _element_to_place(
    element: dict[str, Any],
    center_lat: float,
    center_lon: float,
    radius_m: int,
    *,
    include_jiu_jitsu: bool = False,
) -> Place | None:
    osm_type = element.get("type")
    osm_id = element.get("id")
    if osm_type not in {"node", "way", "relation"}:
        return None
    try:
        osm_id_int = int(osm_id)
    except (TypeError, ValueError):
        return None

    lat, lon = _element_coordinates(element)
    if lat is None or lon is None:
        return None

    distance = distance_meters(center_lat, center_lon, lat, lon)
    if distance > radius_m:
        return None

    tags = element.get("tags")
    if not isinstance(tags, dict):
        tags = {}
    name = str(tags.get("name") or tags.get("name:en") or "").strip()
    if not name:
        return None
    category = _category_from_tags(tags, include_jiu_jitsu=include_jiu_jitsu)
    if category is None:
        return None
    tag_pairs = tuple(
        sorted(
            (str(key), str(value))
            for key, value in tags.items()
            if isinstance(key, str) and value is not None
        )
    )
    return Place(
        osm_type=str(osm_type),
        osm_id=osm_id_int,
        name=name,
        latitude=lat,
        longitude=lon,
        distance_m=round(distance, 1),
        category=category,
        tags=tag_pairs,
    )


def _element_coordinates(
    element: dict[str, Any],
) -> tuple[float, float] | tuple[None, None]:
    if "lat" in element and "lon" in element:
        try:
            return float(element["lat"]), float(element["lon"])
        except (TypeError, ValueError):
            return None, None
    center = element.get("center")
    if isinstance(center, dict):
        try:
            return float(center["lat"]), float(center["lon"])
        except (KeyError, TypeError, ValueError):
            return None, None
    return None, None


def _category_from_tags(
    tags: dict[str, Any],
    *,
    include_jiu_jitsu: bool = False,
) -> str | None:
    for key, value in ATTRACTION_TAGS:
        if tags.get(key) == value:
            return f"{key}={value}"
    if include_jiu_jitsu and _matches_jiu_jitsu(tags):
        sports = _sport_tokens(tags)
        if "jiu-jitsu" in sports:
            return "sport=jiu-jitsu"
        if "martial_arts" in sports:
            return "sport=martial_arts"
        for key, value in _JIU_JITSU_HALL_TAGS:
            if tags.get(key) == value:
                return f"{key}={value}"
    return None


def _interest_is_jiu_jitsu(interest: str) -> bool:
    return _JIU_JITSU_NAME_RE.search(_fold_label(interest)) is not None


def _fold_label(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").split())


def _sport_tokens(tags: dict[str, Any]) -> set[str]:
    raw = tags.get("sport")
    if raw is None:
        return set()
    return {part.strip().casefold() for part in str(raw).split(";") if part.strip()}


def _is_jiu_jitsu_hall(tags: dict[str, Any]) -> bool:
    return any(tags.get(key) == value for key, value in _JIU_JITSU_HALL_TAGS)


def _name_indicates_jiu_jitsu(tags: dict[str, Any]) -> bool:
    blob = " ".join(
        str(tags.get(key) or "")
        for key in ("name", "name:en", "name:ru", "alt_name")
    )
    return _JIU_JITSU_NAME_RE.search(_fold_label(blob)) is not None


def _matches_jiu_jitsu(tags: dict[str, Any]) -> bool:
    """sport-тег сам по себе достаточен. Зал без тега — только по названию."""
    if _sport_tokens(tags) & _JIU_JITSU_SPORTS:
        return True
    return _is_jiu_jitsu_hall(tags) and _name_indicates_jiu_jitsu(tags)


# Тип места для текста агентам. Смысл имени не переводится.
_CATEGORY_RU = {
    "tourism=museum": "музей",
    "tourism=attraction": "достопримечательность",
    "tourism=viewpoint": "смотровая площадка",
    "tourism=gallery": "галерея",
    "tourism=zoo": "зоопарк",
    "historic=monument": "памятник",
    "historic=castle": "замок",
    "historic=archaeological_site": "археологический объект",
    "natural=beach": "пляж",
    "leisure=nature_reserve": "заповедник",
    "sport=jiu-jitsu": "джиу-джитсу",
    "sport=martial_arts": "единоборства",
    "amenity=dojo": "додзё",
    "leisure=fitness_centre": "фитнес-центр",
    "leisure=sports_centre": "спортивный центр",
    "leisure=sports_hall": "спортивный зал",
}

def place_category_label(category: str) -> str:
    """Русское имя типа OSM. Неизвестный тег не подменяется выдуманным типом."""
    return _CATEGORY_RU.get(category, category)


def place_display_name(place: Place) -> str:
    """Одно читаемое имя для исследования и маршрута.

    Латиница и кириллица остаются как в OSM. Для иной письменности сначала
    name:en, иначе локальная транслитерация anyascii. Оригинал в скобках.
    """
    tags = dict(place.tags)
    local = str(tags.get("name") or "").strip()
    name_en = str(tags.get("name:en") or "").strip()
    if not local:
        local = place.name.strip()
    return _readable_place_name(local, name_en)


def _readable_place_name(local: str, name_en: str) -> str:
    local = " ".join(local.split())
    name_en = " ".join(name_en.split())
    if local and _letter_script(local) != "other":
        return local
    if not local:
        return name_en
    head = _latin_label(name_en) or _latin_label(_transliterate(local))
    if not head or head.casefold() == local.casefold():
        return local
    return f"{head} ({local})"


def _latin_label(text: str) -> str:
    """Латинская подпись: хотя бы одна буква ASCII, без перевода смысла."""
    cleaned = " ".join(text.split())
    if cleaned and any(char.isascii() and char.isalpha() for char in cleaned):
        return cleaned
    return ""


def _transliterate(text: str) -> str:
    """Таблица anyascii. Пустой результат значит, что латинское имя не получилось."""
    try:
        return anyascii(text)
    except (TypeError, ValueError):
        return ""


def _letter_script(text: str) -> str:
    """latin, cyrillic или other. Знаки и цифры письменность не меняют."""
    saw_other = False
    for char in text:
        if not char.isalpha():
            continue
        if _is_latin_letter(char):
            continue
        if _is_cyrillic_letter(char):
            continue
        saw_other = True
    return "other" if saw_other else "latin"


def _is_latin_letter(char: str) -> bool:
    return unicodedata.name(char, "").startswith("LATIN")


def _is_cyrillic_letter(char: str) -> bool:
    return "CYRILLIC" in unicodedata.name(char, "")


def _build_query(
    latitude: float,
    longitude: float,
    radius_m: int,
    *,
    include_jiu_jitsu: bool = False,
) -> str:
    # Компактный запрос: nwr + регулярки вместо 30 отдельных селекторов.
    # Публичный Overpass часто отдаёт 504 на тяжёлых around-запросах.
    around = f"(around:{radius_m},{latitude},{longitude})"
    parts = [
        f'  nwr["tourism"~"^(museum|attraction|viewpoint|gallery|zoo)$"]{around};',
        f'  nwr["historic"~"^(monument|castle|archaeological_site)$"]{around};',
        f'  nwr["natural"="beach"]{around};',
        f'  nwr["leisure"="nature_reserve"]{around};',
    ]
    if include_jiu_jitsu:
        # Залы без sport-тега запрашиваем только по имени; отсев дублирует Python.
        name_re = "jiu[- ]?jitsu|джиу[- ]?джитсу|bjj"
        parts.append(
            '  nwr["sport"~"^(.*;)?(jiu-jitsu|martial_arts)(;.*)?$"]'
            f"{around};"
        )
        for name_key in ("name", "name:en", "name:ru", "alt_name"):
            name = f'["{name_key}"~"{name_re}",i]'
            parts.append(f'  nwr["amenity"="dojo"]{name}{around};')
            parts.append(
                '  nwr["leisure"~"^(fitness_centre|sports_centre|sports_hall)$"]'
                f"{name}{around};"
            )
    body = "\n".join(parts)
    return (
        "[out:json][timeout:25];\n"
        "(\n"
        f"{body}\n"
        ");\n"
        "out center;"
    )


_OVERPASS_FALLBACKS: tuple[str, ...] = (
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/cgi/interpreter",
)

# Верхняя граница одного поиска Overpass, если вызывающий не передал свой остаток.
_RUN_BUDGET_SEC = 60.0

# Пауза по умолчанию при HTTP 429, если нет заголовка Retry-After.
_DEFAULT_RETRY_AFTER_SEC = 15.0

# Ниже этого остатка новый HTTP-запрос не начинаем.
_MIN_CALL_SEC = 0.5


class _OverpassDeadline:
    """Общий предел времени одного run_overpass."""

    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def remaining(self) -> float:
        return self._end - time.monotonic()


def _places_failure(kind: OverpassFailure, cause: BaseException | None = None) -> PlacesError:
    error = PlacesError(overpass_user_message(kind), kind=kind)
    if cause is not None:
        raise error from cause
    raise error


def _retry_after_seconds(error: urllib.error.HTTPError) -> float:
    raw = error.headers.get("Retry-After") if error.headers else None
    if raw is None:
        return _DEFAULT_RETRY_AFTER_SEC
    text = str(raw).strip()
    try:
        return max(float(text), 1.0)
    except ValueError:
        return _DEFAULT_RETRY_AFTER_SEC


def _sleep_within(seconds: float, deadline: _OverpassDeadline) -> bool:
    """Пауза не дольше остатка бюджета. False — времени на следующий запрос нет."""
    pause = min(max(seconds, 0.0), max(deadline.remaining(), 0.0))
    if pause > 0:
        time.sleep(pause)
    return deadline.remaining() >= _MIN_CALL_SEC


def _post_overpass(
    endpoint: str,
    data: bytes,
    user_agent: str,
    deadline: _OverpassDeadline,
) -> str:
    _wait_for_rate_limit(deadline)
    timeout = deadline.remaining()
    if timeout < _MIN_CALL_SEC:
        raise TimeoutError
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def _parse_overpass_elements(payload: str) -> list[dict[str, Any]]:
    try:
        data_json = json.loads(payload)
    except json.JSONDecodeError as error:
        raise PlacesError("Overpass вернул не JSON.") from error
    elements = data_json.get("elements") if isinstance(data_json, dict) else None
    if not isinstance(elements, list):
        raise PlacesError("Неожиданный формат ответа Overpass.")
    return [item for item in elements if isinstance(item, dict)]


def _fetch_from_endpoint(
    endpoint: str,
    data: bytes,
    user_agent: str,
    deadline: _OverpassDeadline,
) -> list[dict[str, Any]]:
    """Один endpoint. При 429 — одна пауза и повтор на том же адресе, без зеркал.

    Таймаут HTTP не больше остатка общего бюджета.
    """
    if deadline.remaining() < _MIN_CALL_SEC:
        _places_failure(OverpassFailure.TIMEOUT)
    try:
        payload = _post_overpass(endpoint, data, user_agent, deadline)
    except urllib.error.HTTPError as error:
        if error.code != 429:
            raise
        wait = min(_retry_after_seconds(error), max(deadline.remaining(), 0.0))
        if not _sleep_within(wait, deadline):
            _places_failure(OverpassFailure.RATE_LIMIT, error)
        try:
            payload = _post_overpass(endpoint, data, user_agent, deadline)
        except urllib.error.HTTPError as retry_error:
            if retry_error.code == 429:
                _places_failure(OverpassFailure.RATE_LIMIT, retry_error)
            raise
        except urllib.error.URLError as retry_error:
            _places_failure(OverpassFailure.NETWORK, retry_error)
        except TimeoutError as retry_error:
            _places_failure(OverpassFailure.TIMEOUT, retry_error)
        return _parse_overpass_elements(payload)
    except urllib.error.URLError as error:
        _places_failure(OverpassFailure.NETWORK, error)
    except TimeoutError as error:
        _places_failure(OverpassFailure.TIMEOUT, error)
    return _parse_overpass_elements(payload)


def _one_fallback(primary: str) -> str | None:
    """Не больше одного запасного сервера, отличного от основного."""
    for endpoint in _OVERPASS_FALLBACKS:
        if endpoint.rstrip("/") != primary:
            return endpoint
    return None


def _failure_from_http(error: urllib.error.HTTPError) -> None:
    if error.code == 429:
        _places_failure(OverpassFailure.RATE_LIMIT, error)
    if error.code == 504:
        _places_failure(OverpassFailure.OVERLOAD, error)
    _places_failure(OverpassFailure.OTHER, error)


def run_overpass(
    overpass_query: str,
    settings: OsmSettings | None = None,
    *,
    budget_sec: float | None = None,
) -> list[dict[str, Any]]:
    """Один поиск Overpass в пределах budget_sec (по умолчанию 60 секунд).

    budget_sec — оставшееся время общего поиска: запросы, паузы и один
    запасной сервер при HTTP 504 не выходят за этот остаток.
    HTTP 429 — повтор только на том же сервере, без зеркал.
    """
    limit = _RUN_BUDGET_SEC if budget_sec is None else budget_sec
    if limit < _MIN_CALL_SEC:
        _places_failure(OverpassFailure.TIMEOUT)
    deadline = _OverpassDeadline(limit)
    resolved = load_osm_settings() if settings is None else settings
    data = urllib.parse.urlencode({"data": overpass_query}).encode("utf-8")
    primary = resolved.overpass_url.rstrip("/")
    user_agent = resolved.user_agent

    try:
        return _fetch_from_endpoint(primary, data, user_agent, deadline)
    except urllib.error.HTTPError as error:
        if error.code != 504:
            _failure_from_http(error)
        fallback = _one_fallback(primary)
        if fallback is None or deadline.remaining() < _MIN_CALL_SEC:
            _places_failure(OverpassFailure.OVERLOAD, error)
        try:
            return _fetch_from_endpoint(fallback, data, user_agent, deadline)
        except urllib.error.HTTPError as fallback_error:
            _failure_from_http(fallback_error)
        except PlacesError:
            raise
    except PlacesError:
        raise


def _fetch_overpass(
    latitude: float,
    longitude: float,
    radius_m: int,
    settings: OsmSettings,
    *,
    include_jiu_jitsu: bool = False,
) -> list[dict[str, Any]]:
    query = _build_query(
        latitude,
        longitude,
        radius_m,
        include_jiu_jitsu=include_jiu_jitsu,
    )
    return run_overpass(query, settings=settings)


def _wait_for_rate_limit(deadline: _OverpassDeadline) -> None:
    global _last_request_at
    now = time.monotonic()
    elapsed = now - _last_request_at
    if _last_request_at > 0 and elapsed < _MIN_REQUEST_INTERVAL_SEC:
        if not _sleep_within(_MIN_REQUEST_INTERVAL_SEC - elapsed, deadline):
            raise TimeoutError
    elif deadline.remaining() < _MIN_CALL_SEC:
        raise TimeoutError
    _last_request_at = time.monotonic()
