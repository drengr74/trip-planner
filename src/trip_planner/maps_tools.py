"""Инструменты CrewAI поверх OSM-клиентов (Overpass и OSRM driving)."""

from __future__ import annotations

from crewai.tools import BaseTool, tool

from .geocode import GeocodeResult, settlement_name
from .places_osm import (
    OSM_INTEREST_NOT_FOUND,
    PlacesError,
    jiu_jitsu_interests,
    place_category_label,
    place_display_name,
    place_matches_jiu_jitsu,
    search_places,
)
from .routes_osrm import ROUTE_SOURCE_LABEL, RoutesError, travel_time

# Чтобы не раздувать ответ агента на больших городах.
_MAX_PLACES = 40


def format_map_coordinates(latitude: float, longitude: float) -> str:
    """Широта, долгота для вставки в поиск Google Maps. Без подписей и единиц."""
    return f"{latitude:.5f}, {longitude:.5f}"


def build_researcher_tools(
    destination: GeocodeResult,
    interests: tuple[str, ...] = (),
) -> list[BaseTool]:
    """Поиск достопримечательностей Overpass вокруг выбранного центра."""
    center_latitude = destination.latitude
    center_longitude = destination.longitude
    center_name = settlement_name(destination)
    selected_interests = tuple(interests)

    @tool("search_attractions_near_destination")
    def search_attractions_near_destination() -> str:
        """Найти достопримечательности около центра города назначения через Overpass.

        Возвращает только места с названием, OSM ID, координатами и расстоянием
        до центра в пределах настроенного радиуса. Новые места не выдумывает.
        Для интересов джиу-джитсу и BJJ добавляет подходящие объекты OSM.
        """
        try:
            places = search_places(
                center_latitude,
                center_longitude,
                interests=selected_interests,
            )
        except PlacesError as error:
            return f"Ошибка Overpass: {error.message}"
        martial = [place for place in places if place_matches_jiu_jitsu(place)]
        shown = places[:_MAX_PLACES]
        shown_ids = {(place.osm_type, place.osm_id) for place in shown}
        for place in martial:
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
        for interest in jiu_jitsu_interests(selected_interests):
            if not martial:
                lines.append(
                    f"По интересу «{interest}»: {OSM_INTEREST_NOT_FOUND}"
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
        minutes = estimate.duration_seconds / 60.0
        km = estimate.distance_meters / 1000.0
        return (
            f"От центра {center_name}: в одну сторону около {minutes:.0f} мин, "
            f"{km:.1f} км. {estimate.label}."
        )

    return [estimate_driving_time_from_center]


# Явная ссылка на метку для задач и тестов модулей.
OSRM_TIME_LABEL = ROUTE_SOURCE_LABEL

INTERCITY_TIME_UNVERIFIED = "время не проверено, сверьте по карте"


def format_intercity_driving(
    origin_name: str,
    destination_name: str,
    *,
    origin_latitude: float | None = None,
    origin_longitude: float | None = None,
    destination_latitude: float | None = None,
    destination_longitude: float | None = None,
) -> str:
    """Время между городами только через OSRM driving.

    Без координат отправления или без маршрута число минут не подставляется.
    Расчёт от центра назначения до достопримечательностей здесь не делается.
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
            "Число минут не указывай и не называй расстояние по прямой временем поездки."
        )
    try:
        estimate = travel_time(
            origin_latitude,
            origin_longitude,
            destination_latitude,
            destination_longitude,
        )
    except RoutesError:
        return (
            f"{head}: {INTERCITY_TIME_UNVERIFIED}. "
            "Маршрут OSRM driving не получен, число минут не указывай."
        )
    minutes = estimate.duration_seconds / 60.0
    km = estimate.distance_meters / 1000.0
    return (
        f"{head}: около {minutes:.0f} мин, {km:.1f} км. {estimate.label}."
    )
