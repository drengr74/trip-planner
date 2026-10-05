"""Сборка команды из уже готовых ролей и поручений."""

from crewai import Crew, Process

from .agents import create_agents
from .geocode import GeocodeResult
from .llm import create_llm
from .tasks import create_tasks
from .trip import TripRequest


def create_crew(
    trip: TripRequest,
    destination: GeocodeResult,
    origin: GeocodeResult | None = None,
) -> tuple[Crew, dict[str, object]]:
    """Собирает последовательную команду. Запрос к модели не отправляет."""
    llm = create_llm()
    agents = create_agents(llm, destination, trip.interests)
    tasks = create_tasks(trip, agents, destination, origin)
    crew = Crew(
        agents=[agents.researcher, agents.planner, agents.reviewer],
        tasks=[tasks.research, tasks.itinerary, tasks.review],
        process=Process.sequential,
    )
    return crew, agents.web_state
