"""Запуск планирования для уже готовой поездки."""

from dataclasses import dataclass

from crewai.crews.crew_output import CrewOutput

from .config import ConfigError
from .crew import create_crew
from .geocode import GeocodeError, GeocodeResult, assert_destination_is_route_center
from .maps_tools import (
    _strip_interest_blocks,
    apply_web_source_selection,
    build_route_structure,
    ensure_single_interest_block,
    strip_contradictory_route_claims,
    strip_unselected_web_urls,
)
from .plan_view import PlanView, build_plan_view, format_plan_text
from .trip import TripRequest


class PlanningError(Exception):
    """Не удалось подготовить запуск: геокодинг или настройки OSM."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class PlanResult:
    """Ответ Crew и разбор по вкладкам. raw — текст для консоли."""

    crew: CrewOutput
    view: PlanView

    @property
    def raw(self) -> str:
        return self.crew.raw


def plan_trip(
    trip: TripRequest,
    destination: GeocodeResult,
    origin: GeocodeResult | None = None,
) -> PlanResult:
    """Запускает команду для уже выбранного центра назначения.

    Город повторно не геокодируется. Центр назначения и, если передан,
    центр отправления должны быть населёнными пунктами, не провинцией
    или областью. Без отправления межгородской OSRM не вызывается.
    """
    try:
        assert_destination_is_route_center(destination)
        if origin is not None:
            assert_destination_is_route_center(origin)
    except GeocodeError as error:
        raise PlanningError(error.message) from error
    try:
        crew, web_state = create_crew(trip, destination, origin)
        result = crew.kickoff()
    except ConfigError as error:
        raise PlanningError(error.message) from error
    view = _attach_saved_blocks(result, web_state, trip)
    return PlanResult(crew=result, view=view)


def _attach_saved_blocks(
    result: CrewOutput,
    web_state: dict[str, object],
    trip: TripRequest,
) -> PlanView:
    """Чистит ответы агентов и собирает вкладки. Служебная проверка в текст не попадает."""
    outputs = list(result.tasks_output)
    if outputs:
        research = outputs[0]
        saved = web_state.get("interest_matches")
        text = ensure_single_interest_block(
            research.raw or "",
            saved if isinstance(saved, str) else "",
        )
        research.raw = apply_web_source_selection(text, web_state)

    itinerary = outputs[1].raw if len(outputs) > 1 and outputs[1].raw else ""
    if len(outputs) > 1:
        itinerary = strip_unselected_web_urls(itinerary or "", web_state)
        outputs[1].raw = itinerary
    catalog = web_state.get("place_catalog")
    catalog_list = catalog if isinstance(catalog, list) else None
    report = build_route_structure(itinerary or "", trip.days, catalog_list)
    review_raw = outputs[2].raw if len(outputs) > 2 and outputs[2].raw else ""
    cleaned = strip_unselected_web_urls(
        strip_contradictory_route_claims(
            _strip_interest_blocks(review_raw),
            report,
        ),
        web_state,
    )
    if len(outputs) > 2:
        outputs[2].raw = cleaned
    view = build_plan_view(
        itinerary=itinerary or "",
        review=cleaned,
        state=web_state,
        trip=trip,
        catalog=catalog_list,
    )
    result.raw = format_plan_text(view)
    return view
