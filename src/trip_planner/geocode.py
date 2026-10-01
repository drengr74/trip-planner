"""Однократное геокодирование города через Nominatim с кэшем.

Политика: свой User-Agent с контактом, не чаще одного запроса в секунду,
кэш ответов, без массового геокодинга.

Если рядом населённый пункт и одноимённая провинция — берём населённый пункт
по совпадению имени. Административная область только как контекст, не центр.
Несколько городов с тем же именем — просьба уточнить. Только провинция —
просьба уточнить город.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .config import OsmSettings, load_osm_settings
from .osm_http import urlopen

# Nominatim: не чаще 1 запроса в секунду на публичный сервис.
_MIN_REQUEST_INTERVAL_SEC = 1.0
_SEARCH_LIMIT = 8

# class=place, type=…
_SETTLEMENT_PLACE_TYPES = frozenset(
    {
        "city",
        "town",
        "village",
        "hamlet",
        "municipality",
        "suburb",
        "neighbourhood",
        "quarter",
        "borough",
    }
)

# addresstype у boundary/administrative и place (при addressdetails=1)
_SETTLEMENT_ADDRESS_TYPES = frozenset(
    {
        "city",
        "town",
        "village",
        "hamlet",
        "municipality",
        "suburb",
        "neighbourhood",
        "quarter",
        "borough",
    }
)

_ADMIN_AREA_ADDRESS_TYPES = frozenset(
    {
        "continent",
        "country",
        "state",
        "province",
        "region",
        "state_district",
        "county",
        "municipality_district",
        "city_district",
        "district",
        "ocean",
        "sea",
    }
)

# Суффиксы, которые мешают сравнить «Phuket» с «Phuket City Municipality».
_NAME_SUFFIXES = (
    " city municipality",
    " city",
    " town",
    " village",
    " municipality",
    " province",
    " state",
    " region",
    " county",
    " district",
)

_THAI_PROVINCE_PREFIX = "จังหวัด"

_last_request_at = 0.0


class GeocodeError(Exception):
    """Город не найден или название неоднозначно."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class GeocodeResult:
    query: str
    display_name: str
    latitude: float
    longitude: float
    osm_type: str | None = None
    osm_id: int | None = None
    place_kind: str = ""
    region: str = ""
    country: str = ""
    # Короткое имя пункта для поездки и поручений. Не полный адрес.
    place_name: str = ""


@dataclass(frozen=True)
class DestinationCandidate:
    """Один результат Nominatim для выбора пользователем (до 8, без автовыбора)."""

    query: str
    display_name: str
    place_kind: str
    region: str
    country: str
    latitude: float
    longitude: float
    osm_type: str | None
    osm_id: int | None
    is_settlement: bool
    primary_name: str = ""
    alt_names: tuple[str, ...] = ()
    name_en: str = ""
    name_ru: str = ""


class NameMatchKind(str, Enum):
    """Точное совпадение имени vs похожий вариант (неполное)."""

    EXACT = "exact"
    SIMILAR = "similar"
    NONE = "none"


_cache: dict[str, GeocodeResult] = {}


def clear_geocode_cache() -> None:
    """Очищает кэш успешных ответов (удобно для учебных проверок)."""
    _cache.clear()


def geocode_city(
    city: str,
    settings: OsmSettings | None = None,
) -> GeocodeResult:
    """Вернуть координаты города или населённого пункта.

    На одно имя — не больше одного сетевого запроса. Повтор с тем же названием
    берётся из кэша. При одноимённой провинции выбирается населённый пункт
    с совпадающим именем; область не становится центром маршрута.
    """
    query = " ".join(city.split())
    if not query:
        raise GeocodeError("Введите название города.")

    cache_key = query.casefold()
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    resolved = load_osm_settings() if settings is None else settings
    rows = _fetch_nominatim(query, resolved)
    result = _pick_unique_settlement(query, rows)
    _cache[cache_key] = result
    return result


def search_destination_candidates(
    city: str,
    settings: OsmSettings | None = None,
) -> list[DestinationCandidate]:
    """До 8 совпадений Nominatim без выбора центра и без Overpass.

    Провинции и области могут быть в списке с is_settlement=False;
    их нельзя передать в plan_trip как центр маршрута.
    Fallback по населённым пунктам внутри области — в destination_search.
    """
    query = " ".join(city.split())
    if not query:
        raise GeocodeError("Введите название города.")
    resolved = load_osm_settings() if settings is None else settings
    rows = _fetch_nominatim(query, resolved)
    candidates: list[DestinationCandidate] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        osm_type = str(row.get("osm_type") or "")
        osm_id_raw = row.get("osm_id")
        try:
            osm_id = int(osm_id_raw) if osm_id_raw is not None else None
        except (TypeError, ValueError):
            osm_id = None
        if osm_type and osm_id is not None:
            key = (osm_type, osm_id)
            if key in seen:
                continue
            seen.add(key)
        candidates.append(_to_candidate(query, row))
        if len(candidates) >= _SEARCH_LIMIT:
            break
    return candidates


def candidate_name_matches_query(
    candidate: DestinationCandidate,
    query: str,
) -> bool:
    """True, если есть точное или похожее совпадение по любому имени кандидата."""
    return classify_name_match(candidate, query) != NameMatchKind.NONE


def classify_name_match(
    candidate: DestinationCandidate,
    query: str,
    *,
    aliases: tuple[str, ...] = (),
) -> NameMatchKind:
    """Точное vs похожее совпадение по name / name:* / namedetails.

    «Krabi» / «Краби» и «Krabi Noi» / «Краби Ной» — похожие, не точные.
    Если одно имя точное, а другое — более длинное с тем же префиксом,
    итог SIMILAR (короткий синоним не делает пункт «единственным городом»).
    aliases — доп. формы запроса (например латиница из имён области).
    """
    query_forms = _match_query_forms(query, aliases)
    if not query_forms:
        return NameMatchKind.NONE

    saw_exact = False
    saw_similar = False
    for raw in _candidate_all_names(candidate):
        for q, q_core in query_forms:
            kind = _classify_one_name(raw, q, q_core)
            if kind == NameMatchKind.EXACT:
                saw_exact = True
            elif kind == NameMatchKind.SIMILAR:
                saw_similar = True
    # Короткий точный синоним + длинное «Query …» → не точный населённый пункт.
    if saw_similar:
        return NameMatchKind.SIMILAR
    if saw_exact:
        return NameMatchKind.EXACT
    return NameMatchKind.NONE


def _match_query_forms(
    query: str,
    aliases: tuple[str, ...],
) -> list[tuple[str, str]]:
    """Нормализованные формы запроса и алиасов для сравнения имён."""
    forms: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in (query, *aliases):
        text = " ".join((raw or "").split())
        if not text:
            continue
        q = _normalize_name(text)
        if not q or q in seen:
            continue
        seen.add(q)
        forms.append((q, _core_name(text)))
    return forms


def _classify_one_name(raw: str, q: str, q_core: str) -> NameMatchKind:
    n = _normalize_name(raw)
    n_core = _core_name(raw)
    if not n:
        return NameMatchKind.NONE
    if n == q or n_core == q or n_core == q_core or n == q_core:
        return NameMatchKind.EXACT
    # Неполное: запрос — префикс более длинного имени («krabi» ⊂ «krabi noi»).
    if n.startswith(q + " ") or n.startswith(q + ","):
        return NameMatchKind.SIMILAR
    if q_core and n_core != q_core and (
        n_core.startswith(q_core + " ") or n.startswith(q_core + " ")
    ):
        return NameMatchKind.SIMILAR
    return NameMatchKind.NONE


def candidate_label_title(
    candidate: DestinationCandidate,
    query: str | None = None,
) -> str:
    """Заголовок для UI: язык запроса или en, местное имя рядом; без выдуманных переводов."""
    local = (candidate.primary_name or "").strip()
    name_en = (candidate.name_en or "").strip()
    name_ru = (candidate.name_ru or "").strip()
    q = query or candidate.query

    headline = _headline_for_query(q, local=local, name_en=name_en, name_ru=name_ru)
    if not headline:
        headline = local or candidate.display_name.split(",")[0].strip() or "без названия"

    if local and headline.casefold() != local.casefold():
        return f"{headline} ({local})"
    if local and not name_en and not name_ru:
        return f"{local} · только местное имя"
    if local:
        return local
    return headline


def _headline_for_query(
    query: str,
    *,
    local: str,
    name_en: str,
    name_ru: str,
) -> str:
    """Имя на языке запроса, иначе английское, иначе пусто (останется местное)."""
    if _query_has_cyrillic(query):
        if name_ru:
            return name_ru
        if name_en:
            return name_en
        return ""
    if _query_has_latin(query):
        if name_en:
            return name_en
        if name_ru:
            return name_ru
        # Латиница в запросе совпала с местным именем — это не «перевод».
        if local and _normalize_name(local) == _normalize_name(query):
            return local
        if local and _core_name(local) == _core_name(query):
            return local
        return name_en or ""
    if name_en:
        return name_en
    if name_ru:
        return name_ru
    return ""


def _query_has_cyrillic(value: str) -> bool:
    return any("\u0400" <= char <= "\u04FF" for char in value)


def _query_has_latin(value: str) -> bool:
    return any("a" <= char.casefold() <= "z" for char in value)


def candidate_preferred_name(
    candidate: DestinationCandidate,
    query: str | None = None,
) -> str:
    """Совместимость: то же, что заголовок подписи без суффикса про местное имя."""
    title = candidate_label_title(candidate, query)
    if " · только местное имя" in title:
        return title.split(" · ", 1)[0]
    if " (" in title and title.endswith(")"):
        return title.split(" (", 1)[0]
    return title


def settlement_name(destination: GeocodeResult) -> str:
    """Короткое имя выбранного пункта, без полного адреса Nominatim."""
    name = " ".join((destination.place_name or "").split())
    if name:
        return name
    head = destination.display_name.split(",")[0].strip()
    return " ".join(head.split()) or destination.display_name.strip()


def candidate_to_geocode_result(
    candidate: DestinationCandidate,
    name_query: str | None = None,
) -> GeocodeResult:
    """Превращает выбранного кандидата в центр маршрута.

    place_name — то же короткое имя, что в подписи списка (язык запроса или en).
    Полный адрес в название города не подставляется.
    """
    if not candidate.is_settlement:
        raise GeocodeError(
            "Провинция или административная область не может быть центром "
            "маршрута. Выберите город или населённый пункт."
        )
    label_query = " ".join((name_query or "").split()) or candidate.query
    return GeocodeResult(
        query=candidate.query,
        display_name=candidate.display_name,
        latitude=candidate.latitude,
        longitude=candidate.longitude,
        osm_type=candidate.osm_type,
        osm_id=candidate.osm_id,
        place_kind=candidate.place_kind,
        region=candidate.region,
        country=candidate.country,
        place_name=candidate_preferred_name(candidate, label_query),
    )


def assert_destination_is_route_center(destination: GeocodeResult) -> None:
    """Проверяет, что центр не провинция/область."""
    kind = destination.place_kind.strip().lower()
    if kind in _ADMIN_AREA_ADDRESS_TYPES:
        raise GeocodeError(
            "Провинция или административная область не может быть центром "
            "маршрута."
        )


def _normalize_name(value: str) -> str:
    text = " ".join(value.casefold().split())
    text = text.replace("ё", "е")
    return text


def _core_name(value: str) -> str:
    """Убирает типичные адм. суффиксы для сравнения с запросом пользователя."""
    name = _normalize_name(value)
    if name.startswith(_THAI_PROVINCE_PREFIX):
        name = name[len(_THAI_PROVINCE_PREFIX) :].strip()
    changed = True
    while changed:
        changed = False
        for suffix in _NAME_SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix):
                name = name[: -len(suffix)].strip()
                changed = True
    return name


def _row_names(row: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    raw_name = row.get("name")
    if isinstance(raw_name, str) and raw_name.strip():
        names.add(raw_name.strip())
    namedetails = row.get("namedetails")
    if isinstance(namedetails, dict):
        for key, value in namedetails.items():
            if not isinstance(value, str) or not value.strip():
                continue
            if key == "name" or str(key).startswith("name:"):
                names.add(value.strip())
    display = str(row.get("display_name") or "").strip()
    if display:
        names.add(display.split(",")[0].strip())
    return names


def _name_matches_query(row: dict[str, Any], query: str) -> bool:
    """Точное совпадение имени с запросом (для geocode_city; без «похожих»)."""
    q = _normalize_name(query)
    q_core = _core_name(query)
    for raw in _row_names(row):
        kind = _classify_one_name(raw, q, q_core)
        if kind == NameMatchKind.EXACT:
            return True
    return False


# Префиксы адм. единицы в имени. Nominatim иногда ставит таким объектам
# addresstype city/village, хотя это район или субрайон, а не населённый пункт.
_ADMIN_UNIT_PREFIXES = (
    "ตำบล",  # tambon, субрайон
    "อำเภอ",  # amphoe, район
    "จังหวัด",  # changwat, провинция
    "tambon ",
    "amphoe ",
    "subdistrict ",
)


def _is_settlement(row: dict[str, Any]) -> bool:
    """Город/населённый пункт, не провинция и не прочая адм. область."""
    address_type = str(row.get("addresstype") or "").strip().lower()
    if address_type in _ADMIN_AREA_ADDRESS_TYPES:
        return False
    # Граница субрайона не становится пунктом из-за addresstype=city/village.
    if _is_admin_boundary(row) and _named_as_admin_unit(row):
        return False
    if address_type in _SETTLEMENT_ADDRESS_TYPES:
        return True

    osm_class = str(row.get("class") or "").strip().lower()
    osm_type = str(row.get("type") or "").strip().lower()
    if osm_class == "place" and osm_type in _SETTLEMENT_PLACE_TYPES:
        return True
    return False


def _is_admin_boundary(row: dict[str, Any]) -> bool:
    osm_class = str(row.get("class") or "").strip().lower()
    osm_type = str(row.get("type") or "").strip().lower()
    return osm_class == "boundary" and osm_type == "administrative"


def _named_as_admin_unit(row: dict[str, Any]) -> bool:
    """Имя объекта — район, субрайон или провинция, а не совпавший перевод."""
    values = [str(row.get("name") or "")]
    namedetails = row.get("namedetails")
    if isinstance(namedetails, dict):
        for key in ("name", "name:th", "name:en"):
            value = namedetails.get(key)
            if isinstance(value, str):
                values.append(value)
    display = str(row.get("display_name") or "")
    if display:
        values.append(display.split(",")[0])
    for raw in values:
        text = " ".join(raw.casefold().split())
        if not text:
            continue
        for prefix in _ADMIN_UNIT_PREFIXES:
            if text.startswith(prefix):
                return True
    return False


def _is_admin_area(row: dict[str, Any]) -> bool:
    address_type = str(row.get("addresstype") or "").strip().lower()
    if address_type in _ADMIN_AREA_ADDRESS_TYPES:
        return True
    osm_class = str(row.get("class") or "").strip().lower()
    osm_type = str(row.get("type") or "").strip().lower()
    return osm_class == "boundary" and osm_type == "administrative"


def _pick_unique_settlement(
    query: str, rows: list[dict[str, Any]]
) -> GeocodeResult:
    settlements = [row for row in rows if _is_settlement(row)]
    admin_areas = [row for row in rows if _is_admin_area(row)]
    matched = [row for row in settlements if _name_matches_query(row, query)]
    matched_admins = [row for row in admin_areas if _name_matches_query(row, query)]

    if len(matched) == 1:
        return _to_result(query, matched[0])

    if len(matched) > 1:
        options = "; ".join(_short_label(row) for row in matched[:_SEARCH_LIMIT])
        raise GeocodeError(
            f"Название «{query}» неоднозначно: найдено несколько "
            f"населённых пунктов с таким именем (в разных местах). "
            f"Уточните город (страна или регион). Варианты: {options}."
        )

    if not settlements:
        if admin_areas or matched_admins:
            raise GeocodeError(
                f"Для «{query}» найдена только провинция или административная "
                "область, а не город. Уточните название города "
                "(например, добавьте «город» или страну). "
                "Центр провинции/области для маршрута не используется."
            )
        raise GeocodeError(
            f"Не найден город или населённый пункт для «{query}». "
            "Уточните название."
        )

    # Есть населённые пункты, но имя с запросом не совпало достаточно точно.
    options = "; ".join(_short_label(row) for row in settlements[:_SEARCH_LIMIT])
    raise GeocodeError(
        f"Не удалось однозначно сопоставить «{query}» с населённым пунктом "
        f"по названию. Уточните город. Найденные населённые пункты: {options}. "
        "Провинция или область в качестве центра не используется."
    )


def _short_label(row: dict[str, Any]) -> str:
    name = str(row.get("display_name") or "").strip()
    kind = str(row.get("addresstype") or row.get("type") or "").strip()
    if kind:
        label = f"{name} [{kind}]" if name else kind
    else:
        label = name or "без названия"
    if len(label) > 90:
        return label[:87] + "..."
    return label


def _address_field(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    address = row.get("address")
    if not isinstance(address, dict):
        return ""
    for key in keys:
        value = address.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _place_kind(row: dict[str, Any]) -> str:
    kind = str(row.get("addresstype") or row.get("type") or "").strip()
    return kind


def _candidate_all_names(candidate: DestinationCandidate) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in (
        candidate.primary_name,
        candidate.name_en,
        candidate.name_ru,
        *candidate.alt_names,
        candidate.display_name.split(",")[0].strip(),
    ):
        text = " ".join((raw or "").split())
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(text)
    return ordered


def _names_from_row(row: dict[str, Any]) -> tuple[str, tuple[str, ...], str, str]:
    """primary_name, alt_names, name_en, name_ru из name / namedetails."""
    all_names = sorted(_row_names(row), key=lambda item: item.casefold())
    raw_primary = row.get("name")
    if isinstance(raw_primary, str) and raw_primary.strip():
        primary = raw_primary.strip()
    elif all_names:
        primary = all_names[0]
    else:
        display = str(row.get("display_name") or "").strip()
        primary = display.split(",")[0].strip() if display else ""

    name_en = ""
    name_ru = ""
    namedetails = row.get("namedetails")
    if isinstance(namedetails, dict):
        en_raw = namedetails.get("name:en")
        ru_raw = namedetails.get("name:ru")
        if isinstance(en_raw, str) and en_raw.strip():
            name_en = en_raw.strip()
        if isinstance(ru_raw, str) and ru_raw.strip():
            name_ru = ru_raw.strip()

    alts = tuple(
        name
        for name in all_names
        if name.casefold() != primary.casefold()
    )
    return primary, alts, name_en, name_ru


def _to_candidate(query: str, row: dict[str, Any]) -> DestinationCandidate:
    result = _to_result(query, row)
    primary, alts, name_en, name_ru = _names_from_row(row)
    return DestinationCandidate(
        query=result.query,
        display_name=result.display_name,
        place_kind=result.place_kind,
        region=result.region,
        country=result.country,
        latitude=result.latitude,
        longitude=result.longitude,
        osm_type=result.osm_type,
        osm_id=result.osm_id,
        is_settlement=_is_settlement(row),
        primary_name=primary,
        alt_names=alts,
        name_en=name_en,
        name_ru=name_ru,
    )


def _to_result(query: str, row: dict[str, Any]) -> GeocodeResult:
    try:
        latitude = float(row["lat"])
        longitude = float(row["lon"])
    except (KeyError, TypeError, ValueError) as error:
        raise GeocodeError(
            "Nominatim вернул ответ без корректных координат."
        ) from error
    osm_id_raw = row.get("osm_id")
    osm_id: int | None
    try:
        osm_id = int(osm_id_raw) if osm_id_raw is not None else None
    except (TypeError, ValueError):
        osm_id = None
    return GeocodeResult(
        query=query,
        display_name=str(row.get("display_name") or query),
        latitude=latitude,
        longitude=longitude,
        osm_type=str(row["osm_type"]) if row.get("osm_type") else None,
        osm_id=osm_id,
        place_kind=_place_kind(row),
        region=_address_field(
            row, ("state", "region", "state_district", "county", "province")
        ),
        country=_address_field(row, ("country",)),
    )


def _fetch_nominatim(query: str, settings: OsmSettings) -> list[dict[str, Any]]:
    _wait_for_rate_limit()
    params = urllib.parse.urlencode(
        {
            "q": query,
            "format": "json",
            "limit": str(_SEARCH_LIMIT),
            # addresstype и локальные имена (name:en) для выбора населённого пункта.
            "addressdetails": "1",
            "namedetails": "1",
        }
    )
    url = f"{settings.nominatim_url}/search?{params}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raise GeocodeError(
            f"Nominatim ответил ошибкой HTTP {error.code}."
        ) from error
    except urllib.error.URLError as error:
        reason = error.reason
        detail = f" ({reason})" if reason else ""
        raise GeocodeError(
            "Не удалось связаться с Nominatim. Проверьте сеть и NOMINATIM_URL."
            f"{detail}"
        ) from error
    except TimeoutError as error:
        raise GeocodeError("Превышено время ожидания ответа Nominatim.") from error

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as error:
        raise GeocodeError("Nominatim вернул не JSON.") from error
    if not isinstance(data, list):
        raise GeocodeError("Неожиданный формат ответа Nominatim.")
    return [item for item in data if isinstance(item, dict)]


def _wait_for_rate_limit() -> None:
    global _last_request_at
    now = time.monotonic()
    elapsed = now - _last_request_at
    if _last_request_at > 0 and elapsed < _MIN_REQUEST_INTERVAL_SEC:
        time.sleep(_MIN_REQUEST_INTERVAL_SEC - elapsed)
    _last_request_at = time.monotonic()
