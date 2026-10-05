"""Ручная проверка OSM-клиентов без CrewAI и ProxyAPI.

Запуск из корня проекта:
    PYTHONPATH=src python3 -m trip_planner.check_osm

При импорте запросы не отправляются.
Нужен OSM_USER_AGENT в .env.
"""

from __future__ import annotations

import sys

from .config import ConfigError, load_osm_settings
from .geocode import GeocodeError, geocode_city
from .places_osm import PlacesError, search_places
from .routes_osrm import RoutesError, format_osrm_measures, travel_time

# Учебный город для ручной проверки цепочки Nominatim → Overpass → OSRM.
_CHECK_CITY = "Краби"


def main() -> None:
    try:
        settings = load_osm_settings()
        place_center = geocode_city(_CHECK_CITY, settings=settings)
        print(f"Город: {place_center.display_name}")
        print(
            f"Координаты центра: {place_center.latitude:.5f}, "
            f"{place_center.longitude:.5f}"
        )
        print(f"Радиус поиска: {settings.search_radius_m} м")

        places = search_places(
            place_center.latitude,
            place_center.longitude,
            settings=settings,
        )
        print(f"Найдено мест: {len(places)}")

        if not places:
            print(
                "Мест в радиусе нет — запрос автомобильного маршрута OSRM пропущен."
            )
            return

        sample = places[0]
        print(
            f"Пример места: {sample.name} "
            f"({sample.osm_type}/{sample.osm_id}), "
            f"{sample.distance_m:.0f} м от центра"
        )

        route = travel_time(
            place_center.latitude,
            place_center.longitude,
            sample.latitude,
            sample.longitude,
            settings=settings,
        )
        print(
            "OSRM driving до места:\n"
            + format_osrm_measures(route.duration_seconds, route.distance_meters)
        )
    except (ConfigError, GeocodeError, PlacesError, RoutesError) as error:
        print(error.message)
        sys.exit(1)


if __name__ == "__main__":
    main()
