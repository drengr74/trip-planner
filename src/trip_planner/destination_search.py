"""Оркестратор поиска пункта назначения: Nominatim + Overpass внутри области.

geocode.py — только Nominatim. settlements_osm.py — только Overpass.
Автовыбора центра маршрута нет: область — контекст, центр — населённый пункт.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .config import OsmSettings, load_osm_settings
from .geocode import (
    DestinationCandidate,
    GeocodeError,
    NameMatchKind,
    classify_name_match,
    search_destination_candidates,
)
from .places_osm import OverpassFailure, PlacesError, overpass_user_message
from .settlements_osm import search_settlements_in_admin_area

OVERPASS_FRIENDLY_MESSAGE = overpass_user_message(OverpassFailure.OTHER)


class DestinationSearchStatus(str, Enum):
    """Результат оркестрации без автовыбора центра."""

    SETTLEMENTS = "settlements"
    NEED_ADMIN_CONTEXT = "need_admin_context"
    EMPTY = "empty"
    OVERPASS_ERROR = "overpass_error"


@dataclass(frozen=True)
class DestinationSearchResult:
    query: str
    status: DestinationSearchStatus
    settlements: tuple[DestinationCandidate, ...]
    admin_contexts: tuple[DestinationCandidate, ...]
    message: str = ""
    retry_admin: DestinationCandidate | None = None
    # Похожие (неточные) населённые пункты — отдельно, без автовыбора.
    similar_settlements: tuple[DestinationCandidate, ...] = ()


def search_destinations(
    city: str,
    settings: OsmSettings | None = None,
) -> DestinationSearchResult:
    """Nominatim: точные города отдельно; без точного города — выбор региона."""
    query = " ".join(city.split())
    if not query:
        raise GeocodeError("Введите название города.")

    resolved = load_osm_settings() if settings is None else settings
    candidates = search_destination_candidates(query, settings=resolved)

    exact_settlements, similar_settlements = _split_settlements(candidates, query)
    exact_admins = _exact_usable_admins(candidates, query)

    if exact_settlements:
        return DestinationSearchResult(
            query=query,
            status=DestinationSearchStatus.SETTLEMENTS,
            settlements=exact_settlements,
            admin_contexts=(),
            message="",
            similar_settlements=similar_settlements,
        )

    # Нет точного города → не запускаем Overpass сами: пользователь выбирает регион.
    if exact_admins:
        return DestinationSearchResult(
            query=query,
            status=DestinationSearchStatus.NEED_ADMIN_CONTEXT,
            settlements=(),
            admin_contexts=exact_admins,
            message="Уточните регион",
            similar_settlements=similar_settlements,
        )

    other_admins = tuple(
        item
        for item in candidates
        if not item.is_settlement and _admin_boundary_usable(item)
    )
    if other_admins:
        return DestinationSearchResult(
            query=query,
            status=DestinationSearchStatus.NEED_ADMIN_CONTEXT,
            settlements=(),
            admin_contexts=other_admins,
            message="Уточните регион",
            similar_settlements=similar_settlements,
        )

    if similar_settlements:
        return DestinationSearchResult(
            query=query,
            status=DestinationSearchStatus.SETTLEMENTS,
            settlements=(),
            admin_contexts=(),
            message=(
                f"Точного совпадения с «{query}» нет. "
                "Ниже похожие варианты — выберите город вручную."
            ),
            similar_settlements=similar_settlements,
        )

    return DestinationSearchResult(
        query=query,
        status=DestinationSearchStatus.EMPTY,
        settlements=(),
        admin_contexts=(),
        message=(
            f"Не нашлось городов по запросу «{query}». "
            "Проверьте написание или добавьте страну."
        ),
    )


def search_settlements_for_admin_context(
    admin: DestinationCandidate,
    query: str,
    settings: OsmSettings | None = None,
) -> DestinationSearchResult:
    """После явного выбора региона-контекста: населённые пункты внутри границы."""
    search = " ".join(query.split())
    if not search:
        raise GeocodeError("Введите название города.")
    if admin.is_settlement:
        raise GeocodeError(
            "Регион нельзя выбрать как город поездки. "
            "Сначала уточните регион, затем выберите город из списка."
        )
    if not _admin_boundary_usable(admin):
        raise GeocodeError(
            "Этот регион нельзя использовать для поиска городов. "
            "Выберите другой вариант или измените запрос."
        )
    resolved = load_osm_settings() if settings is None else settings
    return _settlements_for_admin(admin, search, settings=resolved)


def search_named_settlement_in_region(
    place_name: str,
    admin: DestinationCandidate,
    settings: OsmSettings | None = None,
) -> DestinationSearchResult:
    """Один запрос Nominatim: «название, регион, страна». Без Overpass и без автовыбора.

    В список попадают только населённые пункты, чей адрес подтверждён
    основным или альтернативным именем выбранной области.
    """
    place = " ".join(place_name.split())
    if not place:
        raise GeocodeError("Введите название населённого пункта.")
    if admin.is_settlement or not _admin_boundary_usable(admin):
        raise GeocodeError(
            "Сначала выберите регион. Область нельзя взять центром маршрута."
        )

    region_label = _region_label_for_query(admin)
    country = " ".join((admin.country or "").split())
    parts = [place]
    if region_label:
        parts.append(region_label)
    if country and _normalize_key(country) != _normalize_key(region_label):
        parts.append(country)
    resolved = load_osm_settings() if settings is None else settings
    candidates = search_destination_candidates(", ".join(parts), settings=resolved)
    matched = tuple(
        item
        for item in candidates
        if item.is_settlement and _settlement_belongs_to_admin(item, admin)
    )
    base_query = " ".join((admin.query or place).split())
    if not matched:
        return DestinationSearchResult(
            query=base_query,
            status=DestinationSearchStatus.OVERPASS_ERROR,
            settlements=(),
            admin_contexts=(admin,),
            message=(
                f"В выбранном регионе не найдено населённого пункта «{place}»."
            ),
            retry_admin=admin,
        )
    return DestinationSearchResult(
        query=base_query,
        status=DestinationSearchStatus.SETTLEMENTS,
        settlements=matched,
        admin_contexts=(admin,),
        message="",
        retry_admin=admin,
    )


def _region_label_for_query(admin: DestinationCandidate) -> str:
    """Один фрагмент региона для строки Nominatim, без названия страны."""
    country = _normalize_key(admin.country)
    for raw in (admin.region, admin.name_en, admin.name_ru, admin.primary_name):
        text = " ".join((raw or "").split())
        if len(text) < 2:
            continue
        text = text.split(",")[0].strip()
        if country and _normalize_key(text) == country:
            continue
        return text
    return admin.display_name.split(",")[0].strip()


def _admin_identity_names(admin: DestinationCandidate) -> tuple[str, ...]:
    """Основное имя области и альтернативы из Nominatim, без страны."""
    country = _normalize_key(admin.country)
    names: list[str] = []
    seen: set[str] = set()
    for raw in (admin.primary_name, admin.name_en, admin.name_ru, *admin.alt_names):
        text = " ".join((raw or "").split())
        if len(text) < 2:
            continue
        key = _normalize_key(text)
        if not key or key == country or key in seen:
            continue
        seen.add(key)
        names.append(text)
    return tuple(names)


def _settlement_belongs_to_admin(
    settlement: DestinationCandidate,
    admin: DestinationCandidate,
) -> bool:
    """True, только если адрес пункта содержит имя выбранной области.

    Собственное имя пункта не считается подтверждением. Если совпадения нет,
    пункт не попадает в список.
    """
    admin_names = _admin_identity_names(admin)
    if not admin_names:
        return False
    evidence: list[str] = []
    if settlement.region.strip():
        evidence.append(settlement.region.strip())
    address_parts = [
        part.strip() for part in settlement.display_name.split(",") if part.strip()
    ]
    evidence.extend(address_parts[1:])
    for text in evidence:
        if any(_same_area_name(text, admin_name) for admin_name in admin_names):
            return True
    return False


_AREA_NAME_SUFFIXES = (" province", " state", " region", " county")


def _core_area_name(value: str) -> str:
    name = _normalize_key(value).replace("ё", "е")
    if name.startswith("จังหวัด"):
        name = name[len("จังหวัด") :].strip()
    for suffix in _AREA_NAME_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)].strip()
    return name


def _same_area_name(evidence: str, admin_name: str) -> bool:
    left = _core_area_name(evidence)
    right = _core_area_name(admin_name)
    if len(left) < 2 or len(right) < 2:
        return False
    if left == right:
        return True
    return right in left.split() or left in right.split()


def _settlements_for_admin(
    admin: DestinationCandidate,
    query: str,
    *,
    settings: OsmSettings,
) -> DestinationSearchResult:
    try:
        found = tuple(
            search_settlements_in_admin_area(admin, query, settings=settings)
        )
    except PlacesError as error:
        message = overpass_user_message(error.kind)
        return DestinationSearchResult(
            query=query,
            status=DestinationSearchStatus.OVERPASS_ERROR,
            settlements=(),
            admin_contexts=(admin,),
            message=message,
            retry_admin=admin,
        )

    aliases = _name_aliases_for_match(query, admin)
    exact, similar = _partition_by_match(found, query, aliases=aliases)
    if exact:
        return DestinationSearchResult(
            query=query,
            status=DestinationSearchStatus.SETTLEMENTS,
            settlements=exact,
            admin_contexts=(),
            message="",
            similar_settlements=similar,
            retry_admin=admin,
        )
    if similar:
        return DestinationSearchResult(
            query=query,
            status=DestinationSearchStatus.SETTLEMENTS,
            settlements=(),
            admin_contexts=(),
            message=(
                f"В регионе нет населённого пункта с точным именем «{query}». "
                "Похожие варианты ниже — выберите город вручную."
            ),
            similar_settlements=similar,
            retry_admin=admin,
        )
    return DestinationSearchResult(
        query=query,
        status=DestinationSearchStatus.EMPTY,
        settlements=(),
        admin_contexts=(admin,),
        message=(
            f"В выбранном регионе не нашлось городов по запросу «{query}». "
            "Уточните название или выберите другой регион."
        ),
        retry_admin=admin,
    )


def _name_aliases_for_match(
    query: str,
    admin: DestinationCandidate,
) -> tuple[str, ...]:
    """Доп. формы имени для сравнения (латиница/местное из области), без выдумок."""
    aliases: list[str] = []
    seen = {_normalize_key(query)}
    for raw in (
        admin.name_en,
        admin.name_ru,
        admin.primary_name,
        *admin.alt_names,
    ):
        text = " ".join((raw or "").split())
        if len(text) < 2:
            continue
        key = _normalize_key(text)
        if key in seen:
            continue
        seen.add(key)
        aliases.append(text)
    return tuple(aliases)


def _normalize_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _split_settlements(
    candidates: list[DestinationCandidate],
    query: str,
) -> tuple[tuple[DestinationCandidate, ...], tuple[DestinationCandidate, ...]]:
    exact: list[DestinationCandidate] = []
    similar: list[DestinationCandidate] = []
    for item in candidates:
        if not item.is_settlement:
            continue
        kind = classify_name_match(item, query)
        if kind == NameMatchKind.EXACT:
            exact.append(item)
        elif kind == NameMatchKind.SIMILAR:
            similar.append(item)
    return tuple(exact), tuple(similar)


def _partition_by_match(
    candidates: tuple[DestinationCandidate, ...],
    query: str,
    *,
    aliases: tuple[str, ...] = (),
) -> tuple[tuple[DestinationCandidate, ...], tuple[DestinationCandidate, ...]]:
    exact: list[DestinationCandidate] = []
    similar: list[DestinationCandidate] = []
    for item in candidates:
        kind = classify_name_match(item, query, aliases=aliases)
        if kind == NameMatchKind.EXACT:
            exact.append(item)
        elif kind == NameMatchKind.SIMILAR:
            similar.append(item)
    return tuple(exact), tuple(similar)


def _exact_usable_admins(
    candidates: list[DestinationCandidate],
    query: str,
) -> tuple[DestinationCandidate, ...]:
    matched: list[DestinationCandidate] = []
    seen: set[tuple[str, int]] = set()
    for item in candidates:
        if item.is_settlement:
            continue
        if not _admin_boundary_usable(item):
            continue
        if classify_name_match(item, query) != NameMatchKind.EXACT:
            continue
        key = (item.osm_type or "", item.osm_id or -1)
        if key in seen:
            continue
        seen.add(key)
        matched.append(item)
    return tuple(matched)


def _admin_boundary_usable(candidate: DestinationCandidate) -> bool:
    osm_type = (candidate.osm_type or "").strip().lower()
    return osm_type in {"relation", "way"} and candidate.osm_id is not None
