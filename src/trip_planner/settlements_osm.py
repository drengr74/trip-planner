"""Населённые пункты внутри административной границы OSM (Overpass, не bbox).

Клиент Overpass для поиска place=* строго внутри area области.
Объединение с Nominatim и автовыбор центра здесь не делаются.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

from .config import OsmSettings
from .places_osm import (
    OverpassFailure,
    PlacesError,
    overpass_user_message,
    run_overpass,
)

# Весь поиск внутри области: оба Overpass-запроса, паузы и запасной сервер.
_AREA_SEARCH_BUDGET_SEC = 60.0
_MIN_REMAINING_SEC = 0.5

if TYPE_CHECKING:
    from .geocode import DestinationCandidate

_MAX_SETTLEMENTS = 8
_OVERPASS_OUT_LIMIT = 16

_SETTLEMENT_PLACE_TAGS = frozenset(
    {
        "city",
        "town",
        "village",
        "municipality",
    }
)

# Первый (лёгкий) проход — без village.
_PLACE_TYPES_PRIMARY = "city|town|municipality"
_PLACE_TYPES_VILLAGE = "village"

_PLACE_TYPE_ORDER = {
    "city": 0,
    "town": 1,
    "municipality": 2,
    "village": 3,
}

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

_OVERPASS_REGEX_SPECIAL = re.compile(r"([.\\^$*+?{}[\]|()])")


def search_settlements_in_admin_area(
    admin: DestinationCandidate,
    query: str,
    *,
    settings: OsmSettings | None = None,
) -> list[DestinationCandidate]:
    """Именованные населённые пункты внутри границы административной области.

    Оба запроса Overpass, паузы и запасной сервер делят один срок в 60 секунд.
    1) city|town|municipality.
    2) village — только если первый успешно вернул пустой список и срок ещё не вышел.
    Ошибка первого запроса второй не запускает.
    """
    from .geocode import DestinationCandidate as DestinationCandidateCls

    search = " ".join(query.split())
    if not search:
        raise PlacesError("Введите строку поиска населённого пункта.")

    if admin.is_settlement:
        raise PlacesError(
            "Для поиска внутри области нужен административный кандидат, "
            "а не населённый пункт. Область нельзя выбрать центром маршрута."
        )

    osm_type = (admin.osm_type or "").strip().lower()
    if osm_type not in {"relation", "way"}:
        raise PlacesError(
            "Границу области нельзя использовать: нужен OSM type "
            "relation или way (для relation — area, для way — map_to_area)."
        )
    if admin.osm_id is None or admin.osm_id < 1:
        raise PlacesError(
            "Границу области нельзя использовать: отсутствует корректный OSM ID."
        )

    name_terms = _collect_name_terms(search, admin)
    primary_query = _build_settlements_in_area_query(
        osm_type=osm_type,
        osm_id=admin.osm_id,
        name_terms=name_terms,
        place_types=_PLACE_TYPES_PRIMARY,
    )
    started = time.monotonic()
    # Ошибка первого запроса пробрасывается: village не вызывается.
    elements = run_overpass(
        primary_query,
        settings=settings,
        budget_sec=_AREA_SEARCH_BUDGET_SEC,
    )

    if not elements:
        remaining = _AREA_SEARCH_BUDGET_SEC - (time.monotonic() - started)
        if remaining < _MIN_REMAINING_SEC:
            raise PlacesError(
                overpass_user_message(OverpassFailure.TIMEOUT),
                kind=OverpassFailure.TIMEOUT,
            )
        village_query = _build_settlements_in_area_query(
            osm_type=osm_type,
            osm_id=admin.osm_id,
            name_terms=name_terms,
            place_types=_PLACE_TYPES_VILLAGE,
        )
        elements = run_overpass(
            village_query,
            settings=settings,
            budget_sec=remaining,
        )

    region = (
        admin.region
        or admin.primary_name
        or admin.display_name.split(",")[0].strip()
    )
    candidates: list[DestinationCandidateCls] = []
    seen: set[tuple[str, int]] = set()
    for element in elements:
        candidate = _element_to_candidate(
            query=search,
            element=element,
            region=region,
            country=admin.country,
            candidate_cls=DestinationCandidateCls,
        )
        if candidate is None:
            continue
        key = (candidate.osm_type or "", candidate.osm_id or -1)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            _name_match_rank(search, item.primary_name or item.display_name),
            _PLACE_TYPE_ORDER.get(item.place_kind, 99),
            item.display_name.casefold(),
        )
    )
    return candidates[:_MAX_SETTLEMENTS]


def _collect_name_terms(query: str, admin: DestinationCandidate) -> list[str]:
    """Короткий набор имён для Overpass: запрос, en/ru, местное имя, ядра.

    Не тащим все alt_names и длинный display_name целиком — это раздувает
    regex и провоцирует 504 на area-запросах. Локальное primary_name сохраняем:
    по нему в OSM часто числятся населённые пункты.
    """
    terms: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        text = " ".join((raw or "").split())
        if len(text) < 2:
            return
        # Отбрасываем «адресные» хвосты вроде «Name, Country».
        if "," in text:
            text = text.split(",", 1)[0].strip()
        if len(text) < 2:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        terms.append(text)
        core = _core_name(text)
        if core and core.casefold() not in seen and len(core) >= 2:
            seen.add(core.casefold())
            terms.append(core)

    add(query)
    add(admin.name_en)
    add(admin.name_ru)
    add(admin.primary_name)
    # Из alt — только короткие языковые варианты (name:th и т.п.), не простыни.
    for alt in admin.alt_names:
        compact = " ".join((alt or "").split())
        if not compact or "," in compact:
            continue
        if len(compact) > 48:
            continue
        add(compact)
    return terms


def _build_settlements_in_area_query(
    *,
    osm_type: str,
    osm_id: int,
    name_terms: list[str],
    place_types: str,
) -> str:
    place_clause = f'["place"~"^({place_types})$"]'
    escaped = [_escape_overpass_regex(term) for term in name_terms if term.strip()]
    if not escaped:
        raise PlacesError("Нет безопасных имён для фильтра Overpass.")
    name_re = "|".join(f"^{part}" for part in escaped)

    if osm_type == "relation":
        area_id = 3_600_000_000 + osm_id
        area_def = f"area({area_id})->.searchArea;"
    else:
        area_def = f"way({osm_id});\nmap_to_area->.searchArea;"

    # Латиница → name:en; остальное → name и name:ru (меньше лишних комбинаций).
    latin_parts = [
        _escape_overpass_regex(term)
        for term in name_terms
        if term.strip() and _is_mostly_latin(term)
    ]
    other_parts = [
        _escape_overpass_regex(term)
        for term in name_terms
        if term.strip() and not _is_mostly_latin(term)
    ]

    selectors: list[str] = []
    if other_parts:
        other_re = "|".join(f"^{part}" for part in other_parts)
        selectors.append(
            f'  nwr{place_clause}["name"~"{other_re}",i](area.searchArea);'
        )
        selectors.append(
            f'  nwr{place_clause}["name:ru"~"{other_re}",i](area.searchArea);'
        )
    if latin_parts:
        latin_re = "|".join(f"^{part}" for part in latin_parts)
        selectors.append(
            f'  nwr{place_clause}["name:en"~"{latin_re}",i](area.searchArea);'
        )
        # Местные имена латиницей тоже бывают в name=.
        selectors.append(
            f'  nwr{place_clause}["name"~"{latin_re}",i](area.searchArea);'
        )
    if not selectors:
        # Запас: все термины по трём ключам (как раньше, но с урезанным списком).
        selectors = [
            f'  nwr{place_clause}["name"~"{name_re}",i](area.searchArea);',
            f'  nwr{place_clause}["name:en"~"{name_re}",i](area.searchArea);',
            f'  nwr{place_clause}["name:ru"~"{name_re}",i](area.searchArea);',
        ]

    body = "\n".join(selectors)
    return (
        "[out:json][timeout:45];\n"
        f"{area_def}\n"
        "(\n"
        f"{body}\n"
        ");\n"
        f"out center tags {_OVERPASS_OUT_LIMIT};"
    )


def _is_mostly_latin(value: str) -> bool:
    letters = [char for char in value if char.isalpha()]
    if not letters:
        return False
    latin = sum(1 for char in letters if "a" <= char.casefold() <= "z")
    return latin * 2 >= len(letters)


def _escape_overpass_regex(value: str) -> str:
    """Экранирует спецсимволы regex для Overpass (кавычки в строке QL)."""
    escaped = _OVERPASS_REGEX_SPECIAL.sub(r"\\\1", value)
    return escaped.replace('"', '\\"')


def _element_to_candidate(
    *,
    query: str,
    element: dict[str, Any],
    region: str,
    country: str,
    candidate_cls: type,
) -> Any:
    osm_type = element.get("type")
    osm_id_raw = element.get("id")
    if osm_type not in {"node", "way", "relation"}:
        return None
    try:
        osm_id = int(osm_id_raw)
    except (TypeError, ValueError):
        return None

    tags = element.get("tags")
    if not isinstance(tags, dict):
        return None
    place_kind = str(tags.get("place") or "").strip().lower()
    if place_kind not in _SETTLEMENT_PLACE_TAGS:
        return None

    primary = str(tags.get("name") or "").strip()
    name_en = str(tags.get("name:en") or "").strip()
    name_ru = str(tags.get("name:ru") or "").strip()
    alts: list[str] = []
    for key in ("name:en", "name:ru", "name:th", "official_name"):
        value = tags.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if text.casefold() != primary.casefold() and text not in alts:
                alts.append(text)
    if not primary:
        primary = name_en or name_ru or (alts[0] if alts else "")
    if not primary:
        return None

    latitude, longitude = _element_coordinates(element)
    if latitude is None or longitude is None:
        return None

    region_country = ", ".join(part for part in (region, country) if part)
    display_name = f"{primary}, {region_country}" if region_country else primary

    return candidate_cls(
        query=query,
        display_name=display_name,
        place_kind=place_kind,
        region=region,
        country=country,
        latitude=latitude,
        longitude=longitude,
        osm_type=str(osm_type),
        osm_id=osm_id,
        is_settlement=True,
        primary_name=primary,
        alt_names=tuple(alts),
        name_en=name_en,
        name_ru=name_ru,
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


def _normalize_name(value: str) -> str:
    text = " ".join(value.casefold().split())
    return text.replace("ё", "е")


def _core_name(value: str) -> str:
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


def _name_match_rank(query: str, name: str) -> int:
    """Меньше — лучше совпадение с запросом пользователя."""
    q = _normalize_name(query)
    q_core = _core_name(query)
    n = _normalize_name(name)
    n_core = _core_name(name)
    if n == q or n_core == q or n_core == q_core:
        return 0
    if n.startswith(q + " ") or n.startswith(q + ","):
        return 1
    if q in n or q_core in n_core:
        return 2
    return 3
