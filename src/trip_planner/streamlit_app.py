"""Компактный браузерный интерфейс для планирования поездки.

Запуск из корня проекта (после установки зависимостей):
    streamlit run streamlit_app.py

Поиск городов — «Найти варианты» и «Найти город отправления».
CrewAI — только «Составить план» после выбора города назначения из списка.
Отправление без выбранного пункта остаётся текстом: координаты в маршрут не идут.
«Проверить поиск мест без AI» вызывает только Overpass и не запускает CrewAI.
"""

import re
from dataclasses import dataclass

import streamlit as st

from trip_planner.config import ConfigError
from trip_planner.destination_search import (
    OVERPASS_FRIENDLY_MESSAGE,
    DestinationSearchResult,
    DestinationSearchStatus,
    search_destinations,
    search_named_settlement_in_region,
    search_settlements_for_admin_context,
)
from trip_planner.geocode import (
    DestinationCandidate,
    GeocodeError,
    GeocodeResult,
    candidate_label_title,
    candidate_to_geocode_result,
    settlement_name,
)
from trip_planner.places_osm import Place, PlacesError, search_places
from trip_planner.plan_view import (
    BUDGET_TITLE,
    INTERCITY_TITLE,
    NO_REMARKS,
    TAB_TITLES,
    PlaceView,
    PlanView,
    place_details,
)
from trip_planner.service import PlanResult, PlanningError, plan_trip
from trip_planner.web_search import WEB_BLOCK_TITLE
from trip_planner.trip import (
    EXPENSE_CATEGORIES,
    BudgetMode,
    TripRequest,
    ValidationError,
    budget_mode_label,
    category_label,
    create_trip,
    format_amount,
    parse_budget_amount,
    parse_city,
    parse_currency,
    parse_days,
    parse_interests,
    parse_optional_text,
    parse_required_text,
)


def _candidate_key(candidate: DestinationCandidate) -> str:
    if candidate.osm_type and candidate.osm_id is not None:
        return f"{candidate.osm_type}:{candidate.osm_id}"
    return f"idx:{candidate.display_name}:{candidate.latitude}:{candidate.longitude}"


def _place_label(candidate: DestinationCandidate, query: str = "") -> str:
    """Название (язык запроса / en + местное) · регион · страна — без OSM ID."""
    title = candidate_label_title(candidate, query or candidate.query)
    parts = [title]
    if candidate.region and candidate.region.casefold() not in title.casefold():
        parts.append(candidate.region)
    if candidate.country:
        parts.append(candidate.country)
    return " · ".join(parts)


_CITY_PLACEHOLDER = "__none__"


@dataclass(frozen=True)
class _CitySlot:
    """Отдельное состояние поиска для отправления и для назначения."""

    prefix: str
    search_key: str
    query_key: str
    version_key: str
    selected_key: str
    list_label: str
    similar_label: str
    selected_caption: str


_DESTINATION_SLOT = _CitySlot(
    prefix="destination",
    search_key="destination_search",
    query_key="destination_search_query",
    version_key="destination_search_version",
    selected_key="route_destination",
    list_label="Город",
    similar_label="Похожие города",
    selected_caption="Выбран город",
)
_ORIGIN_SLOT = _CitySlot(
    prefix="origin",
    search_key="origin_search",
    query_key="origin_search_query",
    version_key="origin_search_version",
    selected_key="route_origin",
    list_label="Город отправления",
    similar_label="Похожие города отправления",
    selected_caption="Выбран город отправления",
)


def _apply_city_choice(
    slot: _CitySlot,
    *,
    selected_key: str,
    by_key: dict[str, DestinationCandidate],
) -> None:
    """Ставит центр только при явном выборе населённого пункта из списка."""
    if (
        not selected_key
        or selected_key == _CITY_PLACEHOLDER
        or selected_key not in by_key
    ):
        st.session_state.pop(slot.selected_key, None)
        return
    try:
        label_query = st.session_state.get(slot.query_key)
        st.session_state[slot.selected_key] = candidate_to_geocode_result(
            by_key[selected_key],
            label_query if isinstance(label_query, str) else None,
        )
    except GeocodeError as error:
        st.session_state.pop(slot.selected_key, None)
        st.error(error.message)


def _clear_city_selection(slot: _CitySlot) -> None:
    st.session_state.pop(slot.selected_key, None)
    st.session_state.pop(slot.search_key, None)
    st.session_state.pop(slot.query_key, None)
    version = st.session_state.get(slot.version_key, 0) + 1
    st.session_state[slot.version_key] = version


def _clear_plan_result() -> None:
    st.session_state.pop("crew_result", None)
    st.session_state.pop("trip", None)


def _store_search_result(slot: _CitySlot, result: DestinationSearchResult) -> None:
    st.session_state[slot.version_key] = st.session_state.get(slot.version_key, 0) + 1
    st.session_state[slot.search_key] = result
    st.session_state[slot.query_key] = result.query
    st.session_state.pop(slot.selected_key, None)


def _run_city_search(slot: _CitySlot, query: str) -> None:
    try:
        result = search_destinations(query)
    except GeocodeError as error:
        _clear_city_selection(slot)
        st.error(error.message)
        return
    _store_search_result(slot, result)


def _run_region_settlement_search(
    slot: _CitySlot,
    admin: DestinationCandidate,
    query: str,
) -> None:
    try:
        result = search_settlements_for_admin_context(admin, query)
    except GeocodeError as error:
        st.error(error.message)
        return
    _store_search_result(slot, result)


def _run_named_settlement_search(
    slot: _CitySlot,
    admin: DestinationCandidate,
    place_name: str,
) -> None:
    """Nominatim по кнопке: конкретный пункт внутри уже выбранного региона."""
    _clear_plan_result()
    st.session_state.pop(slot.selected_key, None)
    try:
        result = search_named_settlement_in_region(place_name, admin)
    except GeocodeError as error:
        st.error(error.message)
        return
    _store_search_result(slot, result)


def _build_trip_from_form(
    origin: str,
    destination: str,
    days_raw: str,
    amount_raw: str,
    currency: str,
    budget_mode_label_value: str,
    selected_categories: list[str],
    interests_raw: str,
    preferred_transport: str,
    backup_transport: str,
    housing_notes: str,
) -> TripRequest:
    mode = (
        BudgetMode.PER_DAY
        if budget_mode_label_value == "на день"
        else BudgetMode.WHOLE_TRIP
    )
    return create_trip(
        origin=parse_city(origin, "город отправления"),
        destination=parse_city(destination, "город назначения"),
        days=parse_days(days_raw),
        budget_amount=parse_budget_amount(amount_raw),
        currency=parse_currency(currency),
        budget_mode=mode,
        expense_categories=selected_categories,
        interests=parse_interests(interests_raw),
        preferred_transport=parse_required_text(
            preferred_transport, "Введите предпочтительный транспорт."
        ),
        backup_transport=parse_optional_text(backup_transport),
        housing_notes=parse_optional_text(housing_notes),
    )


def _show_trip_summary(trip: TripRequest) -> None:
    st.subheader("Условия поездки")
    route_col, days_col, budget_col = st.columns(3)
    route_col.markdown(f"**{trip.origin}** → **{trip.destination}**")
    days_col.markdown(f"**{trip.days}** дн.")
    budget_col.markdown(
        f"**{format_amount(trip.budget_limit())}** {trip.currency} "
        f"({format_amount(trip.budget_amount)} {budget_mode_label(trip.budget_mode)})"
    )
    included = ", ".join(category_label(key) for key in trip.expense_categories)
    excluded = [
        label for key, label in EXPENSE_CATEGORIES if key not in trip.expense_categories
    ]
    details = [
        f"В бюджете: {included}",
        f"Вне бюджета: {', '.join(excluded) if excluded else 'нет'}",
        f"Интересы: {', '.join(trip.interests)}",
        f"Транспорт: {trip.preferred_transport}"
        + (
            f" · запасной: {trip.backup_transport}"
            if trip.backup_transport
            else ""
        ),
    ]
    if trip.housing_notes:
        details.append(f"Жильё: {trip.housing_notes}")
    st.caption(" · ".join(details))


_MARTIAL_ARTS_NOT_BJJ = (
    "Общий тег sport=martial_arts подтверждает единоборства, "
    "но сам по себе не доказывает, что в зале преподают именно BJJ."
)


def _form_interests(raw: str) -> tuple[str, ...]:
    """Интересы из поля формы. Пустое поле не блокирует диагностику."""
    return tuple(" ".join(part.split()) for part in raw.split(",") if part.strip())


def _sport_tokens(place: Place) -> set[str]:
    raw = dict(place.tags).get("sport")
    if raw is None:
        return set()
    return {part.strip().casefold() for part in str(raw).split(";") if part.strip()}


def _general_martial_arts(place: Place) -> bool:
    """sport=martial_arts без sport=jiu-jitsu: единоборства, не доказанный BJJ."""
    sports = _sport_tokens(place)
    return "martial_arts" in sports and "jiu-jitsu" not in sports


def _diagnostic_place_lines(places: list[Place]) -> list[str]:
    lines = [f"{place.name} — {place.category}" for place in places]
    if any(_general_martial_arts(place) for place in places):
        lines.append(_MARTIAL_ARTS_NOT_BJJ)
    return lines


def _check_places_without_ai(
    destination: GeocodeResult, interests_raw: str
) -> None:
    """Overpass вокруг выбранного центра. Без CrewAI и без ProxyAPI."""
    interests = _form_interests(interests_raw)
    try:
        places = search_places(
            destination.latitude,
            destination.longitude,
            interests=interests,
        )
    except PlacesError as error:
        st.error(f"Ошибка Overpass: {error.kind.value}")
        return
    except ConfigError:
        st.error("Не удалось прочитать настройки OSM. Секреты не показаны.")
        return
    st.success(f"Найдено мест: {len(places)}")
    if interests:
        st.caption("Интересы: " + ", ".join(interests))
    if places:
        st.text("\n".join(_diagnostic_place_lines(places)))


def _show_selected_city(slot: _CitySlot, place: GeocodeResult) -> None:
    name = settlement_name(place)
    extras = ", ".join(part for part in (place.region, place.country) if part)
    if extras:
        st.caption(f"{slot.selected_caption}: **{name}** ({extras})")
    else:
        st.caption(f"{slot.selected_caption}: **{name}**")


def _origin_for_plan(form_origin: str) -> GeocodeResult | None:
    """Выбранный центр отправления, только если текст поля совпадает с поиском.

    Без явного выбора координаты не подставляются: город из текста сам не геокодируется.
    """
    route_origin = st.session_state.get(_ORIGIN_SLOT.selected_key)
    if not isinstance(route_origin, GeocodeResult):
        return None
    origin_query = st.session_state.get(_ORIGIN_SLOT.query_key)
    if (
        isinstance(origin_query, str)
        and form_origin.casefold() == origin_query.casefold()
    ):
        return route_origin
    raise ValidationError(
        "Название города отправления изменилось после поиска. "
        "Нажмите «Найти город отправления» снова."
    )


def _show_search_panel(slot: _CitySlot, result: DestinationSearchResult) -> None:
    """Показ результата поиска; сетевые вызовы — только по кнопкам ниже."""
    version = st.session_state.get(slot.version_key, 0)
    query = result.query
    prefix = slot.prefix

    if result.status == DestinationSearchStatus.OVERPASS_ERROR:
        st.warning(result.message or OVERPASS_FRIENDLY_MESSAGE)
        admin = result.retry_admin
        if admin is None and result.admin_contexts:
            admin = result.admin_contexts[0]
        if isinstance(admin, DestinationCandidate):
            st.caption("Укажите конкретный населённый пункт в этом регионе.")
            place_name = st.text_input(
                "Населённый пункт",
                key=f"{prefix}_manual_place_{version}",
            )
            if st.button(
                "Найти в этом регионе",
                key=f"{prefix}_manual_region_search_{version}",
            ):
                with st.spinner("Ищем населённый пункт…"):
                    _run_named_settlement_search(slot, admin, place_name)
                st.rerun()
        return

    if result.status == DestinationSearchStatus.EMPTY:
        st.warning(result.message or "Ничего не найдено. Уточните название.")
        return

    if result.status == DestinationSearchStatus.NEED_ADMIN_CONTEXT:
        st.info(result.message or "Уточните регион")
        regions = list(result.admin_contexts)
        if not regions:
            return
        keys = [_candidate_key(item) for item in regions]
        labels = {
            key: _place_label(item, query) for key, item in zip(keys, regions)
        }
        selected_key = st.selectbox(
            "Регион",
            options=keys,
            format_func=lambda key: labels[key],
            key=f"{prefix}_region_select_{version}",
        )
        chosen_region = next(
            item for item in regions if _candidate_key(item) == selected_key
        )
        if st.button("Показать города", key=f"{prefix}_show_cities_{version}"):
            _clear_plan_result()
            with st.spinner("Ищем города в регионе…"):
                _run_region_settlement_search(slot, chosen_region, query)
            st.rerun()
        if result.similar_settlements:
            st.caption("Похожие населённые пункты (не точное имя):")
            for item in result.similar_settlements:
                st.text(_place_label(item, query))
        return

    # SETTLEMENTS — точные города и отдельно похожие; без автовыбора.
    if result.message:
        st.info(result.message)

    exact = list(result.settlements)
    similar = list(result.similar_settlements)
    if not exact and not similar:
        retry = (
            "Найти город отправления"
            if slot.prefix == "origin"
            else "Найти варианты"
        )
        st.warning(f"Список городов пуст. Нажмите «{retry}» снова.")
        st.session_state.pop(slot.selected_key, None)
        return

    # Сбрасываем центр, пока пользователь явно не выберет пункт.
    st.session_state.pop(slot.selected_key, None)

    if exact:
        exact_keys = [_candidate_key(item) for item in exact]
        exact_labels = {
            key: _place_label(item, query) for key, item in zip(exact_keys, exact)
        }
        exact_by_key = {key: item for key, item in zip(exact_keys, exact)}
        options = [_CITY_PLACEHOLDER, *exact_keys]

        def _exact_format(key: str) -> str:
            if key == _CITY_PLACEHOLDER:
                return "Выберите город…"
            return exact_labels[key]

        selected_key = st.selectbox(
            slot.list_label,
            options=options,
            format_func=_exact_format,
            key=f"{prefix}_city_select_{version}",
        )
        _apply_city_choice(slot, selected_key=selected_key, by_key=exact_by_key)

    if similar:
        st.caption("Похожие варианты (не точное совпадение имени):")
        similar_keys = [_candidate_key(item) for item in similar]
        similar_labels = {
            key: _place_label(item, query) for key, item in zip(similar_keys, similar)
        }
        similar_by_key = {key: item for key, item in zip(similar_keys, similar)}
        similar_options = [_CITY_PLACEHOLDER, *similar_keys]

        def _similar_format(key: str) -> str:
            if key == _CITY_PLACEHOLDER:
                return "Выберите похожий вариант…"
            return similar_labels[key]

        similar_key = st.selectbox(
            slot.similar_label,
            options=similar_options,
            format_func=_similar_format,
            key=f"{prefix}_similar_city_select_{version}",
        )
        # Похожий задаёт центр только при явном выборе и если точный не выбран.
        exact_selected = st.session_state.get(slot.selected_key) is not None
        if not exact_selected:
            _apply_city_choice(slot, selected_key=similar_key, by_key=similar_by_key)


_MD_SPECIAL = re.compile(r"([\\`*_\[\]#<>|$~])")


def _md(text: str) -> str:
    """Текст из данных без случайной Markdown-разметки."""
    return _MD_SPECIAL.sub(r"\\\1", text)


def _with_line_breaks(text: str) -> str:
    """Одиночный перенос строки агента остаётся переносом, а не склейкой абзаца."""
    lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            lines.append(line)
            continue
        if in_fence or not line.strip() or line.lstrip().startswith(("#", "|")):
            lines.append(line)
            continue
        lines.append(line.rstrip() + "  ")
    return "\n".join(lines)


def _place_card(place: PlaceView) -> None:
    with st.container(border=True):
        st.markdown(f"**{_md(place.name)}**")
        details = place_details(place)
        if details:
            st.caption(details)
        if place.coordinates:
            st.code(place.coordinates, language=None)


def _show_research(view: PlanView) -> None:
    st.subheader("Интересы")
    if not view.interests:
        st.caption("Интересы для сопоставления не заданы.")
    for item in view.interests:
        st.markdown(f"**{_md(item.interest)}**: {_md(item.status)}")
        for place in item.places:
            st.markdown(f"- {_md(place.name)} · `{place.osm_id}`")
        if item.confirmed:
            continue
        if item.web:
            st.caption(WEB_BLOCK_TITLE)
            for link in item.web:
                st.markdown(
                    f"- {_md(link.title)}  \n  {link.url}  \n  {_md(link.snippet)}"
                )
        elif item.web_message:
            st.caption(item.web_message)

    st.subheader("Места для маршрута")
    if view.route_places:
        for place in view.route_places:
            _place_card(place)
    else:
        st.caption("В маршруте нет мест с OSM ID из найденных.")

    with st.expander(f"Все найденные места ({len(view.all_places)})"):
        if not view.all_places:
            st.caption("Места с названием и OSM ID не получены.")
        for place in view.all_places:
            _place_card(place)


def _show_route(view: PlanView) -> None:
    st.markdown(_with_line_breaks(view.itinerary) or "_Маршрут пуст_")
    st.subheader(INTERCITY_TITLE)
    st.markdown(_with_line_breaks(view.intercity))


def _show_review(view: PlanView) -> None:
    if view.remarks:
        st.markdown("\n".join(f"- {_md(line)}" for line in view.remarks))
    else:
        st.caption(NO_REMARKS)
    st.subheader(BUDGET_TITLE)
    st.markdown(_with_line_breaks(view.budget))


def _show_result(result: PlanResult) -> None:
    research_tab, route_tab, review_tab = st.tabs(list(TAB_TITLES))
    with research_tab:
        _show_research(result.view)
    with route_tab:
        _show_route(result.view)
    with review_tab:
        _show_review(result.view)


def main() -> None:
    st.set_page_config(page_title="Планировщик поездки", layout="centered")
    st.title("Планировщик поездки")
    st.caption(
        "Один человек. Для назначения нажмите «Найти варианты» и выберите город. "
        "Для отправления нажмите «Найти город отправления» и выберите населённый пункт. "
        "Затем «Составить план»."
    )

    with st.form("trip_form"):
        st.subheader("Маршрут")
        origin_col, destination_col = st.columns(2)
        with origin_col:
            origin = st.text_input("Город отправления")
        with destination_col:
            destination = st.text_input("Город назначения")
        days_raw = st.text_input("Длительность, дней")

        st.subheader("Бюджет")
        amount_col, currency_col = st.columns([2, 1])
        with amount_col:
            amount_raw = st.text_input("Сумма бюджета")
        with currency_col:
            currency = st.text_input("Валюта")
        budget_mode_label_value = st.radio(
            "Схема бюджета",
            options=["на день", "на всю поездку"],
            horizontal=True,
        )
        st.caption("Категории расходов в бюджете")
        selected_categories: list[str] = []
        category_cols = st.columns(2)
        for index, (key, label) in enumerate(EXPENSE_CATEGORIES):
            with category_cols[index % 2]:
                if st.checkbox(label, key=f"expense_{key}"):
                    selected_categories.append(key)

        st.subheader("Интересы и транспорт")
        interests_raw = st.text_input("Интересы через запятую")
        preferred_col, backup_col = st.columns(2)
        with preferred_col:
            preferred_transport = st.text_input("Предпочтительный транспорт")
        with backup_col:
            backup_transport = st.text_input("Запасной транспорт (необязательно)")
        housing_notes = st.text_area(
            "Пожелания по жилью (необязательно)",
            height=80,
        )
        origin_search_col, destination_search_col = st.columns(2)
        with origin_search_col:
            origin_search_clicked = st.form_submit_button("Найти город отправления")
        with destination_search_col:
            search_clicked = st.form_submit_button("Найти варианты")
        plan_clicked = st.form_submit_button("Составить план")
        check_clicked = st.form_submit_button("Проверить поиск мест без AI")

    if origin_search_clicked:
        _clear_plan_result()
        try:
            query = parse_city(origin, "город отправления")
        except ValidationError as error:
            _clear_city_selection(_ORIGIN_SLOT)
            st.error(error.message)
        else:
            with st.spinner("Ищем город отправления…"):
                _run_city_search(_ORIGIN_SLOT, query)

    if search_clicked:
        _clear_plan_result()
        try:
            query = parse_city(destination, "город назначения")
        except ValidationError as error:
            _clear_city_selection(_DESTINATION_SLOT)
            st.error(error.message)
        else:
            with st.spinner("Ищем варианты…"):
                _run_city_search(_DESTINATION_SLOT, query)

    if plan_clicked:
        route_destination = st.session_state.get("route_destination")
        search_query = st.session_state.get("destination_search_query")
        try:
            # Текст поля — только проверка, что запрос не меняли после поиска.
            form_destination = parse_city(destination, "город назначения")
            if route_destination is None:
                _build_trip_from_form(
                    origin=origin,
                    destination=form_destination,
                    days_raw=days_raw,
                    amount_raw=amount_raw,
                    currency=currency,
                    budget_mode_label_value=budget_mode_label_value,
                    selected_categories=selected_categories,
                    interests_raw=interests_raw,
                    preferred_transport=preferred_transport,
                    backup_transport=backup_transport,
                    housing_notes=housing_notes,
                )
                st.error(
                    "Сначала нажмите «Найти варианты» и выберите город "
                    "из списка."
                )
            elif (
                not isinstance(search_query, str)
                or form_destination.casefold() != search_query.casefold()
            ):
                _build_trip_from_form(
                    origin=origin,
                    destination=form_destination,
                    days_raw=days_raw,
                    amount_raw=amount_raw,
                    currency=currency,
                    budget_mode_label_value=budget_mode_label_value,
                    selected_categories=selected_categories,
                    interests_raw=interests_raw,
                    preferred_transport=preferred_transport,
                    backup_transport=backup_transport,
                    housing_notes=housing_notes,
                )
                st.error(
                    "Название города назначения изменилось после поиска. "
                    "Нажмите «Найти варианты» снова."
                )
            else:
                # Назначение — выбранный пункт. Отправление с координатами —
                # только явно выбранный населённый пункт, не свободный текст.
                form_origin = parse_city(origin, "город отправления")
                selected_origin = _origin_for_plan(form_origin)
                origin_name = (
                    settlement_name(selected_origin)
                    if selected_origin is not None
                    else form_origin
                )
                trip = _build_trip_from_form(
                    origin=origin_name,
                    destination=settlement_name(route_destination),
                    days_raw=days_raw,
                    amount_raw=amount_raw,
                    currency=currency,
                    budget_mode_label_value=budget_mode_label_value,
                    selected_categories=selected_categories,
                    interests_raw=interests_raw,
                    preferred_transport=preferred_transport,
                    backup_transport=backup_transport,
                    housing_notes=housing_notes,
                )
                st.session_state["trip"] = trip
                with st.spinner("Агенты составляют план…"):
                    try:
                        st.session_state["crew_result"] = plan_trip(
                            trip, route_destination, selected_origin
                        )
                    except PlanningError as error:
                        st.session_state.pop("crew_result", None)
                        st.error(error.message)
        except ValidationError as error:
            _clear_plan_result()
            st.error(error.message)

    origin_result = st.session_state.get(_ORIGIN_SLOT.search_key)
    if isinstance(origin_result, DestinationSearchResult):
        _show_search_panel(_ORIGIN_SLOT, origin_result)

    route_origin = st.session_state.get(_ORIGIN_SLOT.selected_key)
    if isinstance(route_origin, GeocodeResult):
        _show_selected_city(_ORIGIN_SLOT, route_origin)

    search_result = st.session_state.get(_DESTINATION_SLOT.search_key)
    if isinstance(search_result, DestinationSearchResult):
        _show_search_panel(_DESTINATION_SLOT, search_result)

    route_destination = st.session_state.get(_DESTINATION_SLOT.selected_key)
    if isinstance(route_destination, GeocodeResult):
        _show_selected_city(_DESTINATION_SLOT, route_destination)

    if check_clicked:
        if isinstance(route_destination, GeocodeResult):
            with st.spinner("Ищем места в Overpass…"):
                _check_places_without_ai(route_destination, interests_raw)
        else:
            st.warning("Сначала выберите город из списка.")

    if "trip" in st.session_state:
        _show_trip_summary(st.session_state["trip"])

    crew_result = st.session_state.get("crew_result")
    if isinstance(crew_result, PlanResult):
        st.subheader("Результат")
        _show_result(crew_result)


if __name__ == "__main__":
    main()
