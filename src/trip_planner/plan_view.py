"""Результат планирования по вкладкам: исследование, маршрут, проверка.

Данные берутся из уже сохранённого состояния запуска и ответов агентов.
Новых запросов и новых сведений здесь нет.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .maps_tools import (
    INTEREST_MATCH_TITLE,
    ROUTE_CHECK_TITLE,
    _fold_match_text,
    _ids_in_text,
    _present_days,
)
from .trip import (
    EXPENSE_CATEGORIES,
    TripRequest,
    budget_mode_label,
    category_label,
    format_amount,
)
from .web_search import (
    OSM_INTEREST_CONFIRMED,
    WEB_BLOCK_TITLE,
    WEB_EMPTY,
    WEB_NOT_ON_TOPIC,
    WEB_UNAVAILABLE,
)

TAB_TITLES = ("Исследование", "Маршрут", "Проверка")
INTERCITY_TITLE = "Переезд между городами"
BUDGET_TITLE = "Бюджет и что осталось проверить"
NO_REMARKS = "Замечаний с конкретным противоречием нет."

_REMARK_SKIP = re.compile(
    r"проверк[аи] структуры|правило сопоставления|id есть в списке|"
    r"osm id неизвестен|в маршруте \d+ раз|пустых дней нет|"
    r"мест[ао] есть \(|день \d+:|"
    r"для этого дня подходящих точек не найдено|"
    r"бюджет|цен[аыуеой]|стоимост|расписан|наличи[еяю]|"
    r"веб-поиск не нашёл",
    flags=re.IGNORECASE,
)
_ROUTE_NOTICE = re.compile(
    r"(?i)^(?:#+\s*)?(?:\*\*)?\s*(?:переезд между городами|междугородн\w* (?:участок|переезд|дорог\w*))"
)
_ESTIMATE_REMARK = re.compile(r"(?i)osrm|расчётн|расчетн|оценк|сверить по карте")
_REAL_PROBLEM = re.compile(
    r"(?i)не помечен|без пометки|нет пометки|назван\w*\s+(?:временем|подтвержд)|"
    r"подтвержд\w*\s+(?:время|маршрут)|километр\w*\s+назван|нет строки|"
    r"нет в (?:списке|шагах)|не из шагов|вне шагов|противореч|неподтвержд"
)
_PRAISE = re.compile(
    r"(?i)корректн|верно|правильн|соответству|в порядке|соблюден|"
    r"ошибок нет|замечаний нет|нарушений нет|без нарушений|"
    r"указан\w* как|помечен\w* как|обозначен\w* как|допустим|"
    r"дн\w*\s+заполнен|заполнен\w*\s+дн|все дни|день заполнен"
)
_INTERCITY_HINTS = (
    (
        " Число минут, километры и номера дорог между городами не указывай.",
        "",
    ),
    (
        "Маршрут OSRM driving не получен: число минут, километры "
        "и номера дорог между городами не указывай.",
        "Маршрут OSRM driving не получен.",
    ),
)


@dataclass(frozen=True)
class PlaceView:
    name: str
    osm_id: str
    coordinates: str
    distance_m: int | None
    category: str


@dataclass(frozen=True)
class WebLink:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class InterestView:
    interest: str
    status: str
    confirmed: bool
    places: tuple[PlaceView, ...]
    web: tuple[WebLink, ...]
    web_message: str


@dataclass(frozen=True)
class PlanView:
    interests: tuple[InterestView, ...]
    route_places: tuple[PlaceView, ...]
    all_places: tuple[PlaceView, ...]
    itinerary: str
    intercity: str
    remarks: tuple[str, ...]
    budget: str


def _place_from_record(item: dict[str, object]) -> PlaceView:
    distance = item.get("distance_m")
    return PlaceView(
        name=str(item.get("name") or "").strip(),
        osm_id=str(item.get("osm_id") or "").strip(),
        coordinates=str(item.get("coordinates") or "").strip(),
        distance_m=distance if isinstance(distance, int) else None,
        category=str(item.get("category") or "").strip(),
    )


def _all_places(state: dict[str, object]) -> tuple[PlaceView, ...]:
    records = state.get("shown_places")
    if not isinstance(records, list):
        return ()
    return tuple(
        _place_from_record(item) for item in records if isinstance(item, dict)
    )


def _interest_groups(match_text: str) -> list[tuple[str, str, list[str]]]:
    """Интерес, строка статуса OSM и строки мест. Правило сопоставления не входит."""
    groups: list[tuple[str, str, list[str]]] = []
    for line in match_text.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped == INTEREST_MATCH_TITLE
            or stripped.startswith("Правило сопоставления:")
        ):
            continue
        if stripped.startswith("По интересу «") and "»: " in stripped:
            interest = stripped.split("«", 1)[1].split("»", 1)[0]
            groups.append((interest, stripped, []))
            continue
        if groups:
            groups[-1][2].append(stripped)
    return groups


def _budget_section(trip: TripRequest) -> str:
    included = [category_label(key) for key in trip.expense_categories]
    excluded = [
        label for key, label in EXPENSE_CATEGORIES if key not in trip.expense_categories
    ]
    return "\n".join(
        (
            (
                f"Бюджет: {format_amount(trip.budget_amount)} {trip.currency} "
                f"{budget_mode_label(trip.budget_mode)}."
            ),
            (
                f"Потолок на поездку: {format_amount(trip.budget_limit())} "
                f"{trip.currency}."
            ),
            f"В бюджет входит: {', '.join(included)}.",
            f"Вне бюджета: {', '.join(excluded) if excluded else 'нет'}.",
            "Достаточность бюджета подтвердить нельзя: цены не проверены.",
            "Цены, расписания и наличие мест не подтверждены. Сверьте их сами перед поездкой.",
        )
    )


def _interest_places(
    lines: list[str],
    by_id: dict[str, PlaceView],
) -> tuple[PlaceView, ...]:
    places: list[PlaceView] = []
    for index, line in enumerate(lines):
        if not line.startswith("- "):
            continue
        parts = [part.strip() for part in line[2:].split(" | ")]
        osm_id = parts[1] if len(parts) > 1 else ""
        known = by_id.get(osm_id)
        if known is not None:
            places.append(known)
            continue
        following = lines[index + 1] if index + 1 < len(lines) else ""
        coordinates = following if re.fullmatch(r"-?\d+\.\d{5}, -?\d+\.\d{5}", following) else ""
        places.append(
            PlaceView(
                name=parts[0],
                osm_id=osm_id,
                coordinates=coordinates,
                distance_m=None,
                category=parts[2] if len(parts) > 2 else "",
            )
        )
    return tuple(places)


def _web_for_interest(
    interest: str,
    state: dict[str, object],
) -> tuple[tuple[WebLink, ...], str]:
    records = state.get("web_candidates")
    if not isinstance(records, list):
        return (), WEB_NOT_ON_TOPIC
    key = _fold_match_text(interest)
    record = next(
        (
            item
            for item in records
            if isinstance(item, dict)
            and _fold_match_text(str(item.get("interest") or "")) == key
        ),
        None,
    )
    if not isinstance(record, dict):
        return (), WEB_NOT_ON_TOPIC
    status = record.get("status")
    if status == "unavailable":
        return (), WEB_UNAVAILABLE
    if status == "empty":
        return (), WEB_EMPTY
    selected = {
        url for url in state.get("web_selected_urls") or () if isinstance(url, str)
    }
    links = tuple(
        WebLink(
            title=str(source.get("title") or ""),
            url=source["url"],
            snippet=str(source.get("snippet") or ""),
        )
        for source in record.get("sources", [])
        if isinstance(source, dict)
        and isinstance(source.get("url"), str)
        and source["url"] in selected
    )[:3]
    if not links:
        return (), WEB_NOT_ON_TOPIC
    return links, ""


def _interests(
    state: dict[str, object],
    by_id: dict[str, PlaceView],
) -> tuple[InterestView, ...]:
    saved = state.get("interest_matches")
    groups = _interest_groups(saved if isinstance(saved, str) else "")
    views: list[InterestView] = []
    for interest, status_line, lines in groups:
        status = status_line.split("»: ", 1)[-1]
        confirmed = OSM_INTEREST_CONFIRMED in status
        web: tuple[WebLink, ...] = ()
        message = ""
        if not confirmed:
            web, message = _web_for_interest(interest, state)
        views.append(
            InterestView(
                interest=interest,
                status=status,
                confirmed=confirmed,
                places=_interest_places(lines, by_id) if confirmed else (),
                web=web,
                web_message=message,
            )
        )
    return tuple(views)


def _route_places(
    itinerary: str,
    by_id: dict[str, PlaceView],
) -> tuple[PlaceView, ...]:
    seen: list[str] = []
    for osm_id in _ids_in_text(itinerary):
        if osm_id in by_id and osm_id not in seen:
            seen.append(osm_id)
    return tuple(by_id[osm_id] for osm_id in seen)


def user_intercity(text: str) -> str:
    """Строка OSRM-переезда без служебных указаний агенту."""
    result = text
    for old, new in _INTERCITY_HINTS:
        result = result.replace(old, new)
    return result.strip()


def _strip_intercity_echo(itinerary: str, intercity: str) -> str:
    """В днях не остаётся копия переезда: его строки показываются один раз отдельно."""
    echo = {line.strip() for line in intercity.splitlines() if line.strip()}
    kept: list[str] = []
    for line in itinerary.splitlines():
        stripped = line.strip()
        bare = stripped.lstrip("-* ").strip()
        if stripped and (stripped in echo or bare in echo):
            continue
        if stripped and _ROUTE_NOTICE.match(stripped):
            continue
        kept.append(line)
    return "\n".join(kept)


def useful_remarks(review: str) -> tuple[str, ...]:
    """Только замечания с конкретным противоречием. Успешные проверки не показываются."""
    seen: set[str] = set()
    kept: list[str] = []
    for line in review.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        text = re.sub(r"^(?:[-*•]\s+|\d+[.)]\s+)", "", stripped).strip()
        text = text.strip("*_ ").strip()
        if not text or text.endswith(":"):
            continue
        if text in (ROUTE_CHECK_TITLE, INTEREST_MATCH_TITLE, INTERCITY_TITLE, BUDGET_TITLE):
            continue
        if _REMARK_SKIP.search(text):
            continue
        if _PRAISE.search(text) and not _REAL_PROBLEM.search(text):
            continue
        if _ESTIMATE_REMARK.search(text) and not _REAL_PROBLEM.search(text):
            continue
        key = _fold_match_text(text)
        if key in seen:
            continue
        seen.add(key)
        kept.append(text)
    return tuple(kept)


def build_plan_view(
    *,
    itinerary: str,
    review: str,
    state: dict[str, object],
    trip: TripRequest,
    catalog: list[dict[str, object]] | None,
) -> PlanView:
    all_places = _all_places(state)
    by_id = {place.osm_id: place for place in all_places if place.osm_id}
    raw_intercity = state.get("intercity")
    intercity = user_intercity(raw_intercity) if isinstance(raw_intercity, str) else ""
    days = _present_days(
        _strip_intercity_echo(itinerary, intercity),
        list(catalog or []),
    )
    return PlanView(
        interests=_interests(state, by_id),
        route_places=_route_places(itinerary, by_id),
        all_places=all_places,
        itinerary=days,
        intercity=intercity or "Переезд между городами не посчитан.",
        remarks=useful_remarks(review),
        budget=_budget_section(trip),
    )


def place_details(place: PlaceView) -> str:
    """OSM ID, категория и расстояние — каждое по одному разу."""
    parts = [f"OSM ID {place.osm_id}"] if place.osm_id else []
    if place.category:
        parts.append(place.category)
    if place.distance_m is not None:
        parts.append(f"{place.distance_m} м от центра")
    return " · ".join(parts)


def format_plan_text(view: PlanView) -> str:
    """Тот же результат для консоли: три части в порядке вкладок."""
    research: list[str] = []
    for item in view.interests:
        research.append(f"Интерес «{item.interest}»: {item.status}")
        research.extend(f"- {place.name} ({place.osm_id})" for place in item.places)
        if not item.confirmed:
            if item.web:
                research.append(WEB_BLOCK_TITLE)
                for link in item.web:
                    research.append(f"- {link.title} | {link.url} | {link.snippet}")
            elif item.web_message:
                research.append(item.web_message)
    research.append("Места для маршрута:")
    for place in view.route_places or view.all_places:
        research.append(f"- {place.name} | {place_details(place)}")
        if place.coordinates:
            research.append(place.coordinates)
    remarks = "\n".join(f"- {line}" for line in view.remarks) or NO_REMARKS
    parts = (
        (TAB_TITLES[0], "\n".join(research)),
        (TAB_TITLES[1], f"{view.itinerary}\n\n{INTERCITY_TITLE}\n{view.intercity}"),
        (TAB_TITLES[2], f"{remarks}\n\n{BUDGET_TITLE}\n{view.budget}"),
    )
    return "\n\n".join(f"== {title} ==\n{body.strip()}" for title, body in parts)
