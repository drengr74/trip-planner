"""Инструменты CrewAI поверх OSM-клиентов (Overpass и OSRM driving)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from crewai.tools import BaseTool, tool

from .geocode import GeocodeResult, settlement_name
from .places_osm import (
    Place,
    PlacesError,
    place_category_label,
    format_road_name,
    jiu_jitsu_interests,
    place_display_name,
    place_explicit_jiu_jitsu,
    search_places,
)
from .routes_osrm import (
    ROUTE_SOURCE_LABEL,
    RoadStep,
    RoutesError,
    format_osrm_measures,
    travel_time,
)
from .web_search import (
    OSM_INTEREST_CONFIRMED,
    OSM_INTEREST_UNCONFIRMED,
    WEB_BLOCK_TITLE,
    WEB_EMPTY,
    WEB_NOT_ON_TOPIC,
    WEB_UNAVAILABLE,
    WebSource,
    WebSearchOutcome,
    search_web,
)

# Один ответ Tavily. Модель видит все полученные строки и оставляет до трёх URL.
_WEB_FETCH_LIMIT = 10
_WEB_SHOW_LIMIT = 3
_KEEP_URL_LINE = re.compile(
    r"(?im)^[ \t>*\-]*оставить url(?:\s+по интересу\s+«([^»]*)»)?\s*:\s*(.*)$"
)
_URL_RE = re.compile(r"https?://[^\s|<>\]\)\"'`]+")

# Чтобы не раздувать ответ агента на больших городах.
_MAX_PLACES = 40


def _fold_match_text(text: str) -> str:
    """Пробелы и регистр схлопнуты, ё заменена на е. Смысл не переводится."""
    return " ".join(text.casefold().replace("ё", "е").split())


def _unique_interests(interests: tuple[str, ...]) -> list[str]:
    """Одинаковые формулировки после нормализации. Показывается первое написание."""
    seen: set[str] = set()
    unique: list[str] = []
    for item in interests:
        shown = " ".join(item.split())
        key = _fold_match_text(shown)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(shown)
    return unique


def _place_names(place: Place) -> str:
    tags = dict(place.tags)
    parts = [place.name]
    for key in ("name", "name:en", "name:ru", "alt_name"):
        value = tags.get(key)
        if value:
            parts.append(str(value))
    return " ".join(parts)


INTEREST_MATCH_TITLE = "Соответствие интересов и мест OSM"
INTEREST_MATCH_RULE = (
    "Правило сопоставления: целая формулировка интереса совпадает с подписью тега "
    "места или целиком входит в его название. Смежная или более широкая категория "
    "это не подтверждает. Для интереса джиу-джитсу или BJJ нужно явное подтверждение "
    "в данных места: тег sport=jiu-jitsu или такое имя. "
    "Fire & Boxing Demonstration и Stage сами по себе BJJ не подтверждают."
)


def osm_place_confirms_interest(place: Place, interest: str) -> bool:
    """Единственное правило: подпись тега, целая фраза в имени или явный тег BJJ.

    Смежный тип места узкий интерес не подтверждает. sport=martial_arts не доказывает BJJ.
    """
    folded = _fold_match_text(interest)
    if not folded:
        return False
    if folded == _fold_match_text(place_category_label(place.category)):
        return True
    if folded in _fold_match_text(_place_names(place)):
        return True
    if jiu_jitsu_interests((interest,)) and place_explicit_jiu_jitsu(place):
        return True
    return False


def format_interest_matches(
    interests: tuple[str, ...] | list[str],
    places: list[Place],
) -> str:
    """Один список статусов. Исследователь и проверяющий не решают соответствие заново."""
    lines = [INTEREST_MATCH_TITLE, INTEREST_MATCH_RULE]
    unique = _unique_interests(tuple(interests))
    if not unique:
        lines.append("Интересы для сопоставления не заданы.")
        return "\n".join(lines)
    for interest in unique:
        matched = [
            place for place in places if osm_place_confirms_interest(place, interest)
        ]
        if not matched:
            lines.append(f"По интересу «{interest}»: {OSM_INTEREST_UNCONFIRMED}")
            continue
        lines.append(f"По интересу «{interest}»: {OSM_INTEREST_CONFIRMED}")
        for place in matched:
            lines.append(
                f"- {place_display_name(place)} | {place.osm_type}/{place.osm_id} | "
                f"{place_category_label(place.category)}"
            )
            lines.append(format_map_coordinates(place.latitude, place.longitude))
    return "\n".join(lines)


def ensure_single_interest_block(text: str, saved: str) -> str:
    """В тексте остаётся один канонический блок соответствия, без второй копии."""
    stripped = _strip_interest_blocks(text)
    if not saved.strip():
        return stripped
    current = stripped.rstrip()
    return f"{current}\n\n{saved}" if current else saved


def _strip_interest_blocks(text: str) -> str:
    """Убирает все блоки «Соответствие интересов и мест OSM»."""
    kept: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].strip() != INTEREST_MATCH_TITLE:
            kept.append(lines[index])
            index += 1
            continue
        index += 1
        while index < len(lines) and _is_interest_block_line(lines[index]):
            index += 1
        if index < len(lines) and not lines[index].strip():
            index += 1
    return "\n".join(kept).strip()


def _is_interest_block_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("Правило сопоставления:"):
        return True
    if stripped.startswith("По интересу «"):
        return True
    if stripped == "Интересы для сопоставления не заданы.":
        return True
    if re.fullmatch(r"-?\d+\.\d{5}, -?\d+\.\d{5}", stripped):
        return True
    if stripped.startswith("- ") and re.search(
        r"\b(?:node|way|relation)/\d+\b", stripped, flags=re.IGNORECASE
    ):
        return True
    return False


def _format_interest_web(
    interest: str,
    outcome: WebSearchOutcome,
    *,
    off_topic: bool = False,
) -> str:
    lines = [f"По интересу «{interest}»: {OSM_INTEREST_UNCONFIRMED}"]
    if outcome.unavailable:
        lines.append(WEB_UNAVAILABLE)
        return "\n".join(lines)
    if off_topic:
        lines.append(WEB_NOT_ON_TOPIC)
        return "\n".join(lines)
    if not outcome.sources:
        lines.append(WEB_EMPTY)
        return "\n".join(lines)
    for source in outcome.sources[:_WEB_SHOW_LIMIT]:
        lines.append(
            f"- {source.title} | {source.site} | {source.url} | "
            f"дата проверки {source.checked_on} | {source.snippet}"
        )
    return "\n".join(lines)


def _source_record(source: WebSource) -> dict[str, str]:
    return {
        "title": source.title,
        "site": source.site,
        "url": source.url,
        "checked_on": source.checked_on,
        "snippet": source.snippet,
    }


def _web_reserve_lines(
    interests: tuple[str, ...],
    places: list[Place],
    settlement: str,
    region: str,
    state: dict[str, object],
) -> list[str]:
    """Один базовый веб-запрос на неподтверждённый интерес. Повтор берёт сохранённый текст.

    Смысл заголовка, URL и фрагмента здесь не отсекается: его оценивает
    тот же ответ исследователя. Исходные строки остаются в web_candidates.
    """
    cache = state["cache"]
    if not isinstance(cache, dict):
        return []
    unconfirmed = [
        interest
        for interest in _unique_interests(interests)
        if not any(osm_place_confirms_interest(place, interest) for place in places)
    ]
    if not unconfirmed:
        return []
    records = state.get("web_candidates")
    if not isinstance(records, list):
        records = []
        state["web_candidates"] = records
    lines = [WEB_BLOCK_TITLE]
    for interest in unconfirmed:
        key = _fold_match_text(interest)
        saved = cache.get(key)
        if isinstance(saved, str):
            lines.append(saved)
            continue
        if state.get("blocked"):
            record = {
                "interest": interest,
                "status": "unavailable",
                "sources": [],
                "settlement": settlement,
                "region": region,
            }
            text = _format_web_candidates(record)
            cache[key] = text
            records.append(record)
            lines.append(text)
            continue
        query = " ".join(part for part in (interest, settlement, region) if part.strip())
        outcome = search_web(query, max_results=_WEB_FETCH_LIMIT)
        status = "unavailable" if outcome.unavailable else "empty" if not outcome.sources else "candidates"
        record = {
            "interest": interest,
            "status": status,
            "sources": [_source_record(source) for source in outcome.sources],
            "settlement": settlement,
            "region": region,
        }
        text = _format_web_candidates(record)
        cache[key] = text
        records.append(record)
        if outcome.unavailable:
            state["blocked"] = True
        lines.append(text)
    return lines


def _format_web_candidates(record: dict[str, object]) -> str:
    """Кандидаты для уже идущего ответа исследователя. Итоговый блок собирает код."""
    interest = str(record["interest"])
    lines = [f"По интересу «{interest}»: {OSM_INTEREST_UNCONFIRMED}"]
    status = record["status"]
    if status == "unavailable":
        lines.append(WEB_UNAVAILABLE)
        return "\n".join(lines)
    if status == "empty":
        lines.append(WEB_EMPTY)
        return "\n".join(lines)
    settlement = str(record.get("settlement") or "").strip() or "не указан"
    region = str(record.get("region") or "").strip() or "не указан"
    lines.append(
        "Кандидаты Tavily, ещё не источники ответа. "
        f"Населённый пункт: {settlement}. Регион: {region}. "
        "Оставь адрес только если по заголовку, URL и фрагменту ясно и то, "
        "что содержание относится к этому интересу, и то, что оно относится "
        "к этому пункту или его региону. Синоним, сокращение, перевод и другая "
        "формулировка на другом языке подходят: например, «БЖЖ» может быть описано "
        "как Brazilian jiu-jitsu. Это общее правило для любого интереса. "
        "Совпадения одного города при другой теме недостаточно. "
        "Если связь с темой или местом по этим полям неясна, адрес не оставляй. "
        "Страницу не открывай и другие адреса не добавляй. "
        "Подходящий адрес скопируй дословно в строку «Оставить URL»."
    )
    sources = record.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict):
                continue
            lines.append(
                f"- кандидат | {source.get('title', '')} | {source.get('url', '')} | "
                f"{source.get('snippet', '')}"
            )
    lines.append(f"Оставить URL по интересу «{interest}»:")
    return "\n".join(lines)


def _clean_url(raw: str) -> str:
    return raw.rstrip(".,;:)>»\"'")


def _urls_in_line(line: str) -> list[str]:
    return [_clean_url(match) for match in _URL_RE.findall(line)]


def _chosen_sources(
    text: str,
    records: list[object],
) -> dict[int, list[dict[str, str]]]:
    """URL из строк «Оставить URL», и только если такой адрес уже вернул Tavily."""
    named: dict[str, list[str]] = {}
    unnamed: list[str] = []
    for match in _KEEP_URL_LINE.finditer(text):
        label = _fold_match_text(match.group(1) or "")
        urls = [_clean_url(item) for item in _URL_RE.findall(match.group(2) or "")]
        if label:
            named.setdefault(label, []).extend(urls)
        else:
            unnamed.extend(urls)
    chosen: dict[int, list[dict[str, str]]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            chosen[index] = []
            continue
        sources = [
            source
            for source in record.get("sources", [])
            if isinstance(source, dict) and isinstance(source.get("url"), str)
        ]
        key = _fold_match_text(str(record.get("interest") or ""))
        allowed = named[key] if key in named else unnamed
        allowed_set = set(allowed)
        picked: list[dict[str, str]] = []
        for source in sources:
            url = source["url"]
            if url in allowed_set:
                picked.append(source)
            if len(picked) >= _WEB_SHOW_LIMIT:
                break
        chosen[index] = picked
    return chosen


def _canonical_web_block(
    records: list[object],
    chosen: dict[int, list[dict[str, str]]],
) -> str:
    lines = [WEB_BLOCK_TITLE]
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        interest = str(record.get("interest") or "")
        status = record.get("status")
        if status == "unavailable":
            lines.append(_format_interest_web(interest, WebSearchOutcome(unavailable=True)))
            continue
        if status == "empty":
            lines.append(_format_interest_web(interest, WebSearchOutcome(unavailable=False)))
            continue
        picked = chosen.get(index) or []
        if not picked:
            lines.append(
                _format_interest_web(
                    interest,
                    WebSearchOutcome(unavailable=False),
                    off_topic=True,
                )
            )
            continue
        sources = tuple(
            WebSource(
                title=item["title"],
                site=item["site"],
                url=item["url"],
                checked_on=item["checked_on"],
                snippet=item["snippet"],
            )
            for item in picked
        )
        lines.append(
            _format_interest_web(interest, WebSearchOutcome(unavailable=False, sources=sources))
        )
    return "\n".join(lines)


def _strip_web_echo(text: str, records: list[object]) -> str:
    """Убирает пересказ и чужие URL. Исходные строки добавляются отдельно."""
    status_lines = {
        f"По интересу «{record.get('interest')}»: {OSM_INTEREST_UNCONFIRMED}"
        for record in records
        if isinstance(record, dict)
    }
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if WEB_BLOCK_TITLE in line or "Кандидаты Tavily" in line:
            continue
        if stripped.startswith("- кандидат |") or _KEEP_URL_LINE.match(line):
            continue
        if stripped in status_lines or stripped in {WEB_EMPTY, WEB_UNAVAILABLE, WEB_NOT_ON_TOPIC}:
            continue
        if _urls_in_line(line):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def apply_web_source_selection(text: str, state: dict[str, object]) -> str:
    """Оставляет исходные заголовок, URL и фрагмент только для выбранных адресов.

    Выбор делает уже полученный ответ исследователя. Нового вызова модели нет.
    Адрес, которого не было в ответе Tavily, в блок не попадает.
    """
    records = state.get("web_candidates")
    if not isinstance(records, list) or not records:
        return text
    chosen = _chosen_sources(text, records)
    state["web_selected_urls"] = tuple(
        source["url"] for sources in chosen.values() for source in sources
    )
    saved = state.get("interest_matches")
    body = _strip_interest_blocks(text) if isinstance(saved, str) and saved.strip() else text
    body = _strip_web_echo(body, records)
    chunks = [part for part in (body.strip(),) if part]
    if isinstance(saved, str) and saved.strip():
        chunks.append(saved.strip())
    chunks.append(_canonical_web_block(records, chosen))
    return "\n\n".join(chunks)


def strip_unselected_web_urls(text: str, state: dict[str, object]) -> str:
    """В замечаниях не остаётся отброшенный или придуманный веб-адрес."""
    records = state.get("web_candidates")
    if not isinstance(records, list) or not records:
        return text
    selected = {
        url
        for url in state.get("web_selected_urls") or ()
        if isinstance(url, str)
    }
    kept: list[str] = []
    for line in text.splitlines():
        if _KEEP_URL_LINE.match(line):
            continue
        urls = _urls_in_line(line)
        if urls and any(url not in selected for url in urls):
            if re.search(r"\b(?:node|way|relation)/\d+\b", line, flags=re.IGNORECASE):
                kept.append(line)
            continue
        kept.append(line)
    return "\n".join(kept).strip()


ROUTE_CHECK_TITLE = "Проверка структуры маршрута"
_EMPTY_DAY_PHRASE = "для этого дня подходящих точек не найдено"
_TRANSFER_LINE = re.compile(
    r"междугородн|расчётные шаги osrm|номер и название дороги|"
    r"пробки, перекрытия|переезды между точками дня|"
    r"названия и номера дорог указаны только",
    flags=re.IGNORECASE,
)
_OSM_ID_RE = re.compile(r"\b(node|way|relation)/(\d+)\b", flags=re.IGNORECASE)
_DAY_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:[#>*\-]+\s*)?(?:\*\*|__)?\s*день\s+(\d+)\b[^\n]*$"
)
_EMPTY_DAY_CLAIM = re.compile(
    r"(?i)пуст\w*\s+д|дн\w*\s+пуст|план\s+содержит\s+пуст"
)
_MISSING_ID_CLAIM = re.compile(
    r"(?i)(нет|без|отсутств\w*|не\s+имеет|не\s+указан\w*)"
    r".{0,48}(?:osm\s*)?id|нет\s+идентификатор"
)
_REPEAT_CLAIM = re.compile(r"(?i)повтор")
_NOT_REPEAT = re.compile(r"(?i)не\s+повтор|нет\s+повтор|без\s+повтор")


@dataclass(frozen=True)
class RouteStructure:
    """Факты, которые считаются по тексту маршрута и списку мест, не моделью."""

    text: str
    days_resolved: bool
    empty_days: tuple[int, ...]
    repeated_ids: tuple[str, ...]
    once_names: tuple[str, ...]
    catalog_names: tuple[str, ...]


def _catalog_entry(place: Place) -> dict[str, object]:
    display = place_display_name(place)
    names: list[str] = []
    for raw in (display, place.name):
        folded = _fold_match_text(raw)
        if len(folded) >= 4 and folded not in names:
            names.append(folded)
    return {
        "osm_type": place.osm_type,
        "osm_id": int(place.osm_id),
        "name": display or place.name,
        "names": names,
    }


def _catalog_key(entry: dict[str, object]) -> str:
    return f"{entry['osm_type']}/{entry['osm_id']}"


def _ids_in_text(text: str) -> list[str]:
    found: list[str] = []
    for match in _OSM_ID_RE.finditer(text):
        found.append(f"{match.group(1).lower()}/{int(match.group(2))}")
    return found


def _day_sections(itinerary: str) -> dict[int, str] | None:
    matches = list(_DAY_HEADING_RE.finditer(itinerary))
    if not matches:
        return None
    sections: dict[int, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(itinerary)
        number = int(match.group(1))
        chunk = itinerary[match.end() : end]
        sections[number] = sections.get(number, "") + "\n" + chunk
    return sections


def _names_in_text(catalog: list[dict[str, object]], text: str) -> list[dict[str, object]]:
    folded = _fold_match_text(text)
    found: list[dict[str, object]] = []
    for entry in catalog:
        names = entry.get("names")
        if not isinstance(names, list):
            continue
        if any(isinstance(name, str) and name in folded for name in names):
            found.append(entry)
    return found


def _unknown_point_lines(section: str, catalog: list[dict[str, object]]) -> list[str]:
    """Строка дня без OSM ID и без имени из списка. ID для неё неизвестен."""
    unknown: list[str] = []
    for raw in section.splitlines():
        stripped = raw.strip()
        if not stripped.startswith(("- ", "* ")):
            continue
        body = stripped[2:].strip()
        if not body or _OSM_ID_RE.search(body):
            continue
        if _TRANSFER_LINE.search(body) or re.search(
            r"https?://|расчёт|осрм|osrm|\bмин\b|\bкм\b|дорог",
            body,
            flags=re.IGNORECASE,
        ):
            continue
        if re.fullmatch(r"-?\d+\.\d{5}, -?\d+\.\d{5}", body):
            continue
        if _names_in_text(catalog, body):
            continue
        unknown.append(body)
    return unknown


def build_route_structure(
    itinerary: str,
    days: int,
    catalog: list[dict[str, object]] | None,
) -> RouteStructure:
    """Считает OSM ID, повторы и пустые дни только по тексту маршрута и списку мест."""
    places = list(catalog or [])
    by_id = {_catalog_key(entry): entry for entry in places}
    lines = [ROUTE_CHECK_TITLE]
    if not itinerary.strip():
        lines.append("Текст маршрута неизвестен. Пустые дни и повторы не установлены.")
        return RouteStructure(
            "\n".join(lines),
            False,
            (),
            (),
            (),
            tuple(str(entry["name"]) for entry in places),
        )
    sections = _day_sections(itinerary)
    empty: list[int] = []
    if sections is None:
        lines.append("Разбиение маршрута по дням неизвестно. Пустые дни не установлены.")
        day_text = ""
        days_resolved = False
    else:
        days_resolved = True
        day_text = "\n".join(sections.values())
        for number in range(1, max(days, 0) + 1):
            section = sections.get(number)
            if section is None:
                lines.append(
                    f"День {number}: раздел в маршруте не найден. "
                    "Пуст ли день, неизвестно."
                )
                continue
            ids = _ids_in_text(section)
            named = _names_in_text(places, section)
            if not ids and not named:
                empty.append(number)
                lines.append(
                    f"День {number}: пустой день. Можно сократить поездку "
                    "или отдельно найти дополнительные места."
                )
                continue
            shown = ", ".join(dict.fromkeys(ids)) if ids else "имена есть, OSM ID в разделе нет"
            lines.append(f"День {number}: места есть ({shown}).")
        if not empty and all(number in sections for number in range(1, days + 1)):
            lines.append("Пустых дней нет.")
    id_source = day_text if sections is not None else itinerary
    id_list = _ids_in_text(id_source)
    counts: dict[str, int] = {}
    for osm_id in id_list:
        counts[osm_id] = counts.get(osm_id, 0) + 1
    repeated: list[str] = []
    once_names: list[str] = []
    for osm_id, count in counts.items():
        entry = by_id.get(osm_id)
        name = str(entry["name"]) if entry else ""
        label = f"{osm_id}, {name}".rstrip(", ")
        if entry is None:
            lines.append(f"{osm_id}: в списке исследователя этого ID нет.")
            continue
        if count > 1:
            repeated.append(osm_id)
            lines.append(
                f"{label}: в маршруте {count} раз. "
                "Это одно место, не несколько достопримечательностей."
            )
        else:
            if name:
                once_names.append(name)
            lines.append(
                f"{label}: в маршруте 1 раз. ID есть в списке исследователя."
            )
    route_ids = set(counts)
    for entry in _names_in_text(places, day_text):
        osm_id = _catalog_key(entry)
        if osm_id not in route_ids:
            lines.append(
                f"{entry['name']}: в списке исследователя есть {osm_id}. "
                "В тексте маршрута этот ID не найден. "
                "Утверждать, что у места нет ID, нельзя."
            )
    if sections is not None:
        for number, section in sections.items():
            if number < 1 or number > days:
                continue
            for body in _unknown_point_lines(_without_transfer_lines(section), places):
                lines.append(f"«{body}»: OSM ID неизвестен.")
    return RouteStructure(
        "\n".join(lines),
        days_resolved,
        tuple(empty),
        tuple(repeated),
        tuple(once_names),
        tuple(str(entry["name"]) for entry in places),
    )


def _line_mentions_name(line: str, name: str) -> bool:
    folded_line = _fold_match_text(line)
    folded_name = _fold_match_text(name)
    return len(folded_name) >= 4 and folded_name in folded_line


def strip_contradictory_route_claims(review: str, report: RouteStructure) -> str:
    """Убирает замечания, которые противоречат уже посчитанным фактам."""
    kept: list[str] = []
    known = [name for name in report.catalog_names if name]
    for line in review.splitlines():
        if "Пустых дней нет." in report.text and _EMPTY_DAY_CLAIM.search(line):
            continue
        if _MISSING_ID_CLAIM.search(line) and any(
            _line_mentions_name(line, name) for name in known
        ):
            continue
        if _REPEAT_CLAIM.search(line) and not _NOT_REPEAT.search(line):
            if not report.repeated_ids:
                continue
            named_once = [
                name for name in report.once_names if _line_mentions_name(line, name)
            ]
            mentions_repeat = any(osm_id in line.casefold() for osm_id in report.repeated_ids)
            if named_once and not mentions_repeat:
                continue
        kept.append(line)
    return "\n".join(kept).strip()


def _without_transfer_lines(text: str) -> str:
    """Дорожные шаги переезда не являются точками дня."""
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        if _DAY_HEADING_RE.match(line):
            skipping = False
            kept.append(line)
            continue
        if _TRANSFER_LINE.search(line):
            skipping = True
            continue
        if skipping:
            if not line.strip():
                skipping = False
            elif line.strip().startswith(("- ", "* ")):
                continue
            else:
                skipping = False
        if skipping:
            continue
        kept.append(line)
    return "\n".join(kept)


def _section_has_catalog_id(section: str, catalog: list[dict[str, object]]) -> bool:
    known = {_catalog_key(entry) for entry in catalog}
    return any(osm_id in known for osm_id in _ids_in_text(section))


def _present_days(itinerary: str, catalog: list[dict[str, object]]) -> str:
    """Дни без фразы «точек не найдено», если в дне уже есть ID исследователя."""
    body = _without_transfer_lines(itinerary)
    matches = list(_DAY_HEADING_RE.finditer(body))
    if not matches:
        return _drop_false_empty_phrase(body, catalog).strip()
    chunks: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        section = body[match.start() : end]
        chunks.append(_drop_false_empty_phrase(section, catalog).strip())
    return "\n\n".join(chunk for chunk in chunks if chunk)


def _drop_false_empty_phrase(section: str, catalog: list[dict[str, object]]) -> str:
    if not _section_has_catalog_id(section, catalog):
        return section
    kept = [
        line
        for line in section.splitlines()
        if _EMPTY_DAY_PHRASE not in line.casefold()
    ]
    return "\n".join(kept)


def format_map_coordinates(latitude: float, longitude: float) -> str:
    """Широта, долгота для вставки в поиск Google Maps. Без подписей и единиц."""
    return f"{latitude:.5f}, {longitude:.5f}"


def build_researcher_tools(
    destination: GeocodeResult,
    interests: tuple[str, ...] = (),
    web_state: dict[str, object] | None = None,
) -> list[BaseTool]:
    """Поиск достопримечательностей Overpass вокруг выбранного центра."""
    center_latitude = destination.latitude
    center_longitude = destination.longitude
    center_name = settlement_name(destination)
    selected_interests = tuple(interests)
    # Кэш веб-ответов живёт один запуск Crew: инструмент создаётся вместе с командой.
    if web_state is None:
        web_state = {"blocked": False, "cache": {}}

    @tool("search_attractions_near_destination")
    def search_attractions_near_destination() -> str:
        """Найти достопримечательности около центра города назначения через Overpass.

        Возвращает только места с названием, OSM ID, координатами и расстоянием
        до центра в пределах настроенного радиуса. Новые места не выдумывает.
        Блок соответствия интересов — единственный список подходящих мест.
        Смежный тип не делает объект подходящим. Веб-блок не является объектом OSM.
        """
        try:
            places = search_places(
                center_latitude,
                center_longitude,
                interests=selected_interests,
            )
        except PlacesError as error:
            return f"Ошибка Overpass: {error.message}"
        explicit = [
            place
            for place in places
            if any(
                osm_place_confirms_interest(place, interest)
                for interest in jiu_jitsu_interests(selected_interests)
            )
        ]
        shown = places[:_MAX_PLACES]
        shown_ids = {(place.osm_type, place.osm_id) for place in shown}
        for place in explicit:
            if (place.osm_type, place.osm_id) not in shown_ids:
                shown.append(place)
                shown_ids.add((place.osm_type, place.osm_id))
        if not shown:
            lines = [
                "В радиусе поиска Overpass не нашёл достопримечательностей "
                "с именем, координатами и OSM ID."
            ]
        else:
            lines = [
                f"Найдено мест: {len(shown)} "
                f"(центр {center_name}: {center_latitude:.5f}, {center_longitude:.5f})."
            ]
            for place in shown:
                lines.append(
                    f"- {place_display_name(place)} | {place.osm_type}/{place.osm_id} | "
                    f"{place.distance_m:.0f} м | {place_category_label(place.category)}"
                )
                lines.append(
                    format_map_coordinates(place.latitude, place.longitude)
                )
            hidden = len(places) - len(shown)
            if hidden > 0:
                lines.append(
                    f"… и ещё {hidden} мест ближе к краю радиуса (не показаны)."
                )
        match_text = format_interest_matches(selected_interests, places)
        web_state["interest_matches"] = match_text
        matched_keys = {
            (place.osm_type, place.osm_id)
            for place in places
            if any(
                osm_place_confirms_interest(place, interest)
                for interest in _unique_interests(selected_interests)
            )
        }
        catalog: list[dict[str, object]] = []
        catalog_seen: set[tuple[str, int]] = set()
        for place in [*shown, *places]:
            key = (place.osm_type, place.osm_id)
            if key in catalog_seen:
                continue
            if key not in shown_ids and key not in matched_keys:
                continue
            catalog_seen.add(key)
            catalog.append(_catalog_entry(place))
        web_state["place_catalog"] = catalog
        web_state["shown_places"] = [
            {
                "name": place_display_name(place),
                "osm_id": f"{place.osm_type}/{place.osm_id}",
                "coordinates": format_map_coordinates(place.latitude, place.longitude),
                "distance_m": int(round(place.distance_m)),
                "category": place_category_label(place.category),
            }
            for place in shown
        ]
        lines.append(match_text)
        lines.extend(
            _web_reserve_lines(
                selected_interests,
                places,
                center_name,
                destination.region,
                web_state,
            )
        )
        return "\n".join(lines)

    return [search_attractions_near_destination]


def build_planner_tools(destination: GeocodeResult) -> list[BaseTool]:
    """Оценка времени на авто от выбранного центра назначения (OSRM driving)."""
    center_latitude = destination.latitude
    center_longitude = destination.longitude
    center_name = settlement_name(destination)

    @tool("estimate_driving_time_from_center")
    def estimate_driving_time_from_center(
        end_latitude: float,
        end_longitude: float,
    ) -> str:
        """Оценить время и расстояние на автомобиле от центра города назначения
        до координат места через OSRM (только driving).

        Используй координаты места из списка исследователя. При ошибке или
        отсутствии маршрута время не выдумывается.
        """
        try:
            estimate = travel_time(
                center_latitude,
                center_longitude,
                end_latitude,
                end_longitude,
            )
        except RoutesError as error:
            return (
                f"Маршрут OSRM недоступен: {error.message} "
                "Время в пути не оценивалось — напиши «сверить по карте»."
            )
        measures = format_osrm_measures(
            estimate.duration_seconds,
            estimate.distance_meters,
        )
        return (
            f"От центра {center_name} до точки, в одну сторону.\n"
            f"{measures}\n"
            "Это расчётная оценка, не подтверждённые время и маршрут. "
            "Километры не называй временем."
        )

    return [estimate_driving_time_from_center]


# Явная ссылка на метку для задач и тестов модулей.
OSRM_TIME_LABEL = ROUTE_SOURCE_LABEL

INTERCITY_TIME_UNVERIFIED = "время не проверено, сверьте по карте"
ROAD_DATA_MISSING = "номер и название дороги источник не дал"
OSRM_ROAD_STEPS_LABEL = (
    "Расчётные шаги OSRM. Названия и номера ниже относятся к этим шагам, "
    "а не к проверке пробок, ремонтов или дорожной безопасности."
)
OSRM_ROAD_CONDITIONS_UNCONFIRMED = (
    "Пробки, перекрытия, ремонты и опасности этот маршрут не подтверждает."
)
INTERCITY_ROADS_ONLY = (
    "Названия и номера дорог указаны только для участка между городами. "
    "Переезды между точками дня по дорогам не разбирались."
)


def format_intercity_driving(
    origin_name: str,
    destination_name: str,
    *,
    origin_latitude: float | None = None,
    origin_longitude: float | None = None,
    destination_latitude: float | None = None,
    destination_longitude: float | None = None,
) -> str:
    """Один маршрут OSRM driving между центрами городов, со шагами.

    Без координат отправления или без маршрута минуты, километры и номера
    дорог не подставляются. Расчёт от центра назначения до точек дня здесь
    не делается и шаги для него не запрашиваются.
    """
    head = f"Междугородний участок {origin_name} → {destination_name}"
    if (
        origin_latitude is None
        or origin_longitude is None
        or destination_latitude is None
        or destination_longitude is None
    ):
        return (
            f"{head}: {INTERCITY_TIME_UNVERIFIED}. "
            "Число минут, километры и номера дорог между городами не указывай."
        )
    try:
        estimate = travel_time(
            origin_latitude,
            origin_longitude,
            destination_latitude,
            destination_longitude,
            steps=True,
        )
    except RoutesError:
        return (
            f"{head}: {INTERCITY_TIME_UNVERIFIED}. "
            "Маршрут OSRM driving не получен: число минут, километры "
            "и номера дорог между городами не указывай."
        )
    measures = format_osrm_measures(
        estimate.duration_seconds,
        estimate.distance_meters,
    )
    return "\n".join(
        (
            f"{head}.",
            measures,
            "Это расчётная оценка, не подтверждённые время и маршрут.",
            _format_intercity_roads(estimate.roads),
            OSRM_ROAD_CONDITIONS_UNCONFIRMED,
            INTERCITY_ROADS_ONLY,
        )
    )


def _format_intercity_roads(roads: tuple[RoadStep, ...]) -> str:
    """Непустые name и ref. ref дословно. Иная письменность имени — латиницей и в скобках."""
    if not roads:
        return ROAD_DATA_MISSING
    lines = [OSRM_ROAD_STEPS_LABEL]
    for road in roads:
        name = format_road_name(road.name, road.name_en)
        if road.ref and name:
            lines.append(f"- {road.ref} — {name}")
        elif road.ref:
            lines.append(f"- {road.ref}")
        else:
            lines.append(f"- {name}")
    return "\n".join(lines)
