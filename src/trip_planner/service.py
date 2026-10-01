"""Запуск планирования для уже готовой поездки."""

from crewai.crews.crew_output import CrewOutput

from .config import ConfigError
from .crew import create_crew
from .geocode import GeocodeError, GeocodeResult, assert_destination_is_route_center
from .trip import TripRequest


class PlanningError(Exception):
    """Не удалось подготовить запуск: геокодинг или настройки OSM."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def plan_trip(trip: TripRequest, destination: GeocodeResult) -> CrewOutput:
    """Запускает команду для уже выбранного центра назначения.

    Город повторно не геокодируется. Центр должен быть населённым пунктом,
    не провинцией или областью.
    """
    try:
        assert_destination_is_route_center(destination)
    except GeocodeError as error:
        raise PlanningError(error.message) from error
    try:
        return create_crew(trip, destination).kickoff()
    except ConfigError as error:
        raise PlanningError(error.message) from error
