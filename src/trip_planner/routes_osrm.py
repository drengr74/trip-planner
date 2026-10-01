"""Автомобильный маршрут через публичный OSRM (только профиль driving).

Возвращает длительность и расстояние с пометкой «оценка на авто по OSRM».
При недоступности сервера или отсутствии маршрута время не выдумывается.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import OsmSettings, load_osm_settings
from .osm_http import urlopen

DRIVING_PROFILE = "driving"
ROUTE_SOURCE_LABEL = "оценка на авто по OSRM"


class RoutesError(Exception):
    """Сервер OSRM недоступен или маршрут не найден."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class RouteEstimate:
    duration_seconds: float
    distance_meters: float
    profile: str
    label: str


def travel_time(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
    *,
    settings: OsmSettings | None = None,
) -> RouteEstimate:
    """Оценка пути на автомобиле между двумя точками через OSRM driving."""
    _validate_coordinates(start_latitude, start_longitude, "начала")
    _validate_coordinates(end_latitude, end_longitude, "конца")
    resolved = load_osm_settings() if settings is None else settings
    payload = _fetch_route(
        start_latitude,
        start_longitude,
        end_latitude,
        end_longitude,
        resolved,
    )
    return _parse_route(payload)


def _validate_coordinates(latitude: float, longitude: float, what: str) -> None:
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise RoutesError(f"Координаты {what} должны быть конечными числами.")
    if abs(latitude) > 90 or abs(longitude) > 180:
        raise RoutesError(f"Координаты {what} вне допустимого диапазона.")


def _fetch_route(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
    settings: OsmSettings,
) -> dict[str, Any]:
    # OSRM принимает координаты в порядке lon,lat.
    coordinates = (
        f"{start_longitude},{start_latitude};{end_longitude},{end_latitude}"
    )
    query = urllib.parse.urlencode({"overview": "false"})
    url = (
        f"{settings.osrm_url}/route/v1/{DRIVING_PROFILE}/"
        f"{coordinates}?{query}"
    )
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
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        # OSRM часто отдаёт JSON с code даже при 400 — попробуем разобрать.
        try:
            error_body = error.read().decode("utf-8")
            payload = json.loads(error_body)
            if isinstance(payload, dict):
                return payload
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            pass
        raise RoutesError(
            f"OSRM недоступен или ответил ошибкой HTTP {error.code}."
        ) from error
    except urllib.error.URLError as error:
        reason = error.reason
        detail = f" ({reason})" if reason else ""
        raise RoutesError(
            "Не удалось связаться с OSRM. Проверьте сеть и OSRM_URL."
            f"{detail}"
        ) from error
    except TimeoutError as error:
        raise RoutesError("Превышено время ожидания ответа OSRM.") from error

    try:
        data = json.loads(body)
    except json.JSONDecodeError as error:
        raise RoutesError("OSRM вернул не JSON.") from error
    if not isinstance(data, dict):
        raise RoutesError("Неожиданный формат ответа OSRM.")
    return data


def _parse_route(payload: dict[str, Any]) -> RouteEstimate:
    code = str(payload.get("code") or "").strip()
    if code == "NoRoute":
        raise RoutesError(
            "OSRM не нашёл автомобильный маршрут между этими точками. "
            "Время в пути не оценивалось."
        )
    if code != "Ok":
        message = str(payload.get("message") or "").strip()
        detail = f" ({message})" if message else ""
        raise RoutesError(
            f"OSRM не вернул маршрут (code={code or 'неизвестно'}){detail}. "
            "Время в пути не оценивалось."
        )

    routes = payload.get("routes")
    if not isinstance(routes, list) or not routes:
        raise RoutesError(
            "OSRM не вернул маршруты. Время в пути не оценивалось."
        )
    first = routes[0]
    if not isinstance(first, dict):
        raise RoutesError(
            "Некорректный маршрут в ответе OSRM. Время в пути не оценивалось."
        )
    try:
        duration = float(first["duration"])
        distance = float(first["distance"])
    except (KeyError, TypeError, ValueError) as error:
        raise RoutesError(
            "В ответе OSRM нет длительности или расстояния. "
            "Время в пути не оценивалось."
        ) from error
    if not math.isfinite(duration) or not math.isfinite(distance):
        raise RoutesError(
            "OSRM вернул некорректные числа. Время в пути не оценивалось."
        )
    if duration < 0 or distance < 0:
        raise RoutesError(
            "OSRM вернул отрицательные значения. Время в пути не оценивалось."
        )
    return RouteEstimate(
        duration_seconds=duration,
        distance_meters=distance,
        profile=DRIVING_PROFILE,
        label=ROUTE_SOURCE_LABEL,
    )
