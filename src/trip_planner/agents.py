"""Три роли планировщика поездки. Модель и OSM-инструменты передаются снаружи."""

from dataclasses import dataclass

from crewai import Agent, LLM
from crewai.tools import BaseTool

from .geocode import GeocodeResult
from .maps_tools import build_planner_tools, build_researcher_tools


@dataclass(frozen=True)
class TripAgents:
    researcher: Agent
    planner: Agent
    reviewer: Agent


def create_agents(
    llm: LLM,
    destination: GeocodeResult,
    interests: tuple[str, ...] = (),
) -> TripAgents:
    """Один LLM на всех; исследователю — Overpass, планировщику — OSRM driving."""
    researcher_tools: list[BaseTool] = build_researcher_tools(
        destination, interests
    )
    planner_tools: list[BaseTool] = build_planner_tools(destination)
    researcher = Agent(
        role="Исследователь направления и идей",
        goal=(
            "С помощью инструмента Overpass собрать только реально найденные места "
            "около центра города назначения. Название и тип копировать дословно "
            "из ответа инструмента. Для каждого места указать OSM ID, "
            "координаты, расстояние до центра и метку географии. "
            "Цены и наличие мест помечать как непроверенные."
        ),
        backstory=(
            "Ты готовишь черновик идей для поездки одного человека. "
            "Места берёшь только из инструмента поиска Overpass — "
            "не выдумываешь достопримечательности без OSM ID и координат. "
            "Ты не составляешь расписание по дням и не подтверждаешь бюджет."
        ),
        llm=llm,
        tools=researcher_tools,
        allow_delegation=False,
    )
    planner = Agent(
        role="Планировщик маршрута",
        goal=(
            "Собрать маршрут по дням только из мест, которые нашёл исследователь, "
            "с теми же названиями, OSM ID и координатами. Время до точки считать "
            "инструментом OSRM driving от центра назначения и помечать как оценку "
            "на авто по OSRM. Время между городами не оценивать по прямой."
        ),
        backstory=(
            "Ты раскладываешь найденные OSM-места по дням. "
            "Новые места без данных исследователя не добавляешь. "
            "Время в пути не выдумываешь: только OSRM driving или «сверить по карте»."
        ),
        llm=llm,
        tools=planner_tools,
        allow_delegation=False,
    )
    reviewer = Agent(
        role="Проверяющий бюджет и качество",
        goal=(
            "Проверить маршрут по бюджету, темпу и тому, что в плане только "
            "места с OSM ID из исследования, а время помечено как оценка на авто "
            "по OSRM или «сверить по карте». "
            "Если цены не подтверждены, написать, что достаточность бюджета "
            "подтвердить нельзя."
        ),
        backstory=(
            "Ты сверяешь план с правилом бюджета путешественника. "
            "Поездка в окрестности допустима, если место найдено через OSM. "
            "Оценки цен не называешь фактами."
        ),
        llm=llm,
        tools=[],
        allow_delegation=False,
    )
    return TripAgents(researcher=researcher, planner=planner, reviewer=reviewer)
