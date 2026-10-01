"""Компактный браузерный интерфейс для планирования поездки.

Запуск из корня проекта (после установки зависимостей):
    streamlit run streamlit_app.py

Поиск городов — «Найти варианты». CrewAI — только «Составить план»
после выбора города из списка.
"""

import streamlit as st
from crewai.crews.crew_output import CrewOutput

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
from trip_planner.service import PlanningError, plan_trip
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

TASK_TAB_TITLES = ("Исследование", "Маршрут", "Проверка")


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


def _apply_city_choice(
    *,
    selected_key: str,
    by_key: dict[str, DestinationCandidate],
) -> None:
    """Ставит центр маршрута только при явном выборе пункта из списка."""
    if (
        not selected_key
        or selected_key == _CITY_PLACEHOLDER
        or selected_key not in by_key
    ):
        st.session_state.pop("route_destination", None)
        return
    try:
        label_query = st.session_state.get("destination_search_query")
        st.session_state["route_destination"] = candidate_to_geocode_result(
            by_key[selected_key],
            label_query if isinstance(label_query, str) else None,
        )
    except GeocodeError as error:
        st.session_state.pop("route_destination", None)
        st.error(error.message)


def _clear_destination_selection() -> None:
    st.session_state.pop("route_destination", None)
    st.session_state.pop("destination_search", None)
    st.session_state.pop("destination_search_query", None)
    version = st.session_state.get("destination_search_version", 0) + 1
    st.session_state["destination_search_version"] = version


def _clear_plan_result() -> None:
    st.session_state.pop("crew_result", None)
    st.session_state.pop("trip", None)


def _store_search_result(result: DestinationSearchResult) -> None:
    st.session_state["destination_search_version"] = (
        st.session_state.get("destination_search_version", 0) + 1
    )
    st.session_state["destination_search"] = result
    st.session_state["destination_search_query"] = result.query
    st.session_state.pop("route_destination", None)


def _run_city_search(query: str) -> None:
    try:
        result = search_destinations(query)
    except GeocodeError as error:
        _clear_destination_selection()
        st.error(error.message)
        return
    _store_search_result(result)


def _run_region_settlement_search(
    admin: DestinationCandidate,
    query: str,
) -> None:
    try:
        result = search_settlements_for_admin_context(admin, query)
    except GeocodeError as error:
        st.error(error.message)
        return
    _store_search_result(result)


def _run_named_settlement_search(
    admin: DestinationCandidate,
    place_name: str,
) -> None:
    """Nominatim по кнопке: конкретный пункт внутри уже выбранного региона."""
    _clear_plan_result()
    st.session_state.pop("route_destination", None)
    try:
        result = search_named_settlement_in_region(place_name, admin)
    except GeocodeError as error:
        st.error(error.message)
        return
    _store_search_result(result)


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


def _show_selected_destination(destination: GeocodeResult) -> None:
    place = settlement_name(destination)
    extras = ", ".join(
        part for part in (destination.region, destination.country) if part
    )
    if extras:
        st.caption(f"Выбран город: **{place}** ({extras})")
    else:
        st.caption(f"Выбран город: **{place}**")


def _show_search_panel(result: DestinationSearchResult) -> None:
    """Показ результата поиска; сетевые вызовы — только по кнопкам ниже."""
    version = st.session_state.get("destination_search_version", 0)
    query = result.query

    if result.status == DestinationSearchStatus.OVERPASS_ERROR:
        st.warning(result.message or OVERPASS_FRIENDLY_MESSAGE)
        admin = result.retry_admin
        if admin is None and result.admin_contexts:
            admin = result.admin_contexts[0]
        if isinstance(admin, DestinationCandidate):
            st.caption("Укажите конкретный населённый пункт в этом регионе.")
            place_name = st.text_input(
                "Населённый пункт",
                key=f"manual_place_{version}",
            )
            if st.button("Найти в этом регионе", key=f"manual_region_search_{version}"):
                with st.spinner("Ищем населённый пункт…"):
                    _run_named_settlement_search(admin, place_name)
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
            key=f"region_select_{version}",
        )
        chosen_region = next(
            item for item in regions if _candidate_key(item) == selected_key
        )
        if st.button("Показать города", key=f"show_cities_{version}"):
            _clear_plan_result()
            with st.spinner("Ищем города в регионе…"):
                _run_region_settlement_search(chosen_region, query)
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
        st.warning("Список городов пуст. Нажмите «Найти варианты» снова.")
        st.session_state.pop("route_destination", None)
        return

    # Сбрасываем центр, пока пользователь явно не выберет пункт.
    st.session_state.pop("route_destination", None)

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
            "Город",
            options=options,
            format_func=_exact_format,
            key=f"city_select_{version}",
        )
        _apply_city_choice(selected_key=selected_key, by_key=exact_by_key)

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
            "Похожие города",
            options=similar_options,
            format_func=_similar_format,
            key=f"similar_city_select_{version}",
        )
        # Похожий задаёт центр только при явном выборе и если точный не выбран.
        exact_selected = st.session_state.get("route_destination") is not None
        if not exact_selected:
            _apply_city_choice(selected_key=similar_key, by_key=similar_by_key)


def _show_result(result: CrewOutput) -> None:
    st.info(
        "Цены, расписания и наличие мест не подтверждены: "
        "веб-поиск не подключён. Сверьте их сами перед поездкой."
    )
    outputs = list(result.tasks_output)
    tabs = st.tabs(list(TASK_TAB_TITLES))
    for index, tab in enumerate(tabs):
        with tab:
            if index < len(outputs):
                task = outputs[index]
                st.caption(f"Агент: {task.agent}")
                st.markdown(task.raw or "_Пустой ответ_")
            else:
                st.warning("Для этой вкладки ответа задачи нет.")


def main() -> None:
    st.set_page_config(page_title="Планировщик поездки", layout="centered")
    st.title("Планировщик поездки")
    st.caption(
        "Один человек. Нажмите «Найти варианты», выберите город, "
        "затем «Составить план»."
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
        search_col, plan_col = st.columns(2)
        with search_col:
            search_clicked = st.form_submit_button("Найти варианты")
        with plan_col:
            plan_clicked = st.form_submit_button("Составить план")

    if search_clicked:
        _clear_plan_result()
        try:
            query = parse_city(destination, "город назначения")
        except ValidationError as error:
            _clear_destination_selection()
            st.error(error.message)
        else:
            with st.spinner("Ищем варианты…"):
                _run_city_search(query)

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
                # Фактическое назначение — выбранный пункт, не исходный запрос.
                trip = _build_trip_from_form(
                    origin=origin,
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
                            trip, route_destination
                        )
                    except PlanningError as error:
                        st.session_state.pop("crew_result", None)
                        st.error(error.message)
        except ValidationError as error:
            _clear_plan_result()
            st.error(error.message)

    search_result = st.session_state.get("destination_search")
    if isinstance(search_result, DestinationSearchResult):
        _show_search_panel(search_result)

    route_destination = st.session_state.get("route_destination")
    if route_destination is not None:
        _show_selected_destination(route_destination)

    if "trip" in st.session_state:
        _show_trip_summary(st.session_state["trip"])

    if "crew_result" in st.session_state:
        st.subheader("Результат")
        _show_result(st.session_state["crew_result"])


if __name__ == "__main__":
    main()
