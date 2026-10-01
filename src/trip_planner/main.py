"""Спрашивает параметры поездки и при прямом запуске стартует команду."""

from collections.abc import Callable
from typing import TypeVar

from .geocode import GeocodeError, geocode_city
from .service import PlanningError, plan_trip
from .trip import (
    EXPENSE_CATEGORIES,
    ValidationError,
    create_trip,
    ensure_different_cities,
    format_trip,
    parse_budget_amount,
    parse_budget_mode,
    parse_city,
    parse_currency,
    parse_days,
    parse_expense_selection,
    parse_interests,
    parse_optional_text,
    parse_required_text,
    parse_yes_no,
)

T = TypeVar("T")


def ask_until_valid(prompt: str, parse: Callable[[str], T]) -> T:
    while True:
        raw = input(prompt)
        try:
            return parse(raw)
        except ValidationError as error:
            print(error.message)


def ask_expense_categories() -> tuple[str, ...]:
    while True:
        print("Отметьте, какие расходы входят в бюджет.")
        selected: list[str] = []
        for key, label in EXPENSE_CATEGORIES:
            included = ask_until_valid(f"  {label}? (да/нет): ", parse_yes_no)
            if included:
                selected.append(key)
        try:
            return parse_expense_selection(selected)
        except ValidationError as error:
            print(error.message)


def collect_trip():
    print("Планировщик поездки. Первая версия собирает параметры одного человека.")
    origin = ask_until_valid(
        "Город отправления: ",
        lambda raw: parse_city(raw, "город отправления"),
    )
    destination = ask_until_valid(
        "Город назначения: ",
        lambda raw: _parse_destination(origin, raw),
    )
    days = ask_until_valid("Длительность, дней: ", parse_days)
    budget_amount = ask_until_valid("Сумма бюджета: ", parse_budget_amount)
    currency = ask_until_valid("Валюта: ", parse_currency)
    budget_mode = ask_until_valid(
        "Бюджет «1» — на день, «2» — на всю поездку: ",
        parse_budget_mode,
    )
    expense_categories = ask_expense_categories()
    interests = ask_until_valid("Интересы через запятую: ", parse_interests)
    preferred_transport = ask_until_valid(
        "Предпочтительный транспорт (поезд, автобус, самолёт, автомобиль, паром или свой вариант): ",
        lambda raw: parse_required_text(raw, "Введите предпочтительный транспорт."),
    )
    backup_transport = parse_optional_text(
        input("Запасной транспорт (можно пропустить): ")
    )
    housing_notes = parse_optional_text(
        input("Пожелания по жилью (можно пропустить): ")
    )
    return create_trip(
        origin=origin,
        destination=destination,
        days=days,
        budget_amount=budget_amount,
        currency=currency,
        budget_mode=budget_mode,
        expense_categories=expense_categories,
        interests=interests,
        preferred_transport=preferred_transport,
        backup_transport=backup_transport,
        housing_notes=housing_notes,
    )


def _parse_destination(origin: str, raw: str) -> str:
    destination = parse_city(raw, "город назначения")
    ensure_different_cities(origin, destination)
    return destination


def main() -> None:
    trip = collect_trip()
    print()
    print(format_trip(trip))
    print()
    try:
        destination = geocode_city(trip.destination)
        result = plan_trip(trip, destination)
    except (GeocodeError, PlanningError) as error:
        print(error.message)
        return
    print(result.raw)


if __name__ == "__main__":
    main()
