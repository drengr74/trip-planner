"""Параметры поездки одного человека и проверка полей."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum


class ValidationError(Exception):
    """Поле пустое или введено некорректно."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class BudgetMode(Enum):
    PER_DAY = "per_day"
    WHOLE_TRIP = "whole_trip"


# Первая версия считает поездку одного человека и не спрашивает число людей.
TRAVELER_COUNT = 1

EXPENSE_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("intercity", "дорога между городами"),
    ("food", "еда"),
    ("local_transport", "местный транспорт"),
    ("activities", "активности и билеты"),
    ("housing", "жильё"),
)

_PER_DAY_ANSWERS = {"1", "день", "на день", "в день"}
_WHOLE_TRIP_ANSWERS = {"2", "на всю поездку", "вся поездка"}
_YES_ANSWERS = {"да", "д", "yes", "y"}
_NO_ANSWERS = {"нет", "н", "no", "n"}


@dataclass(frozen=True)
class TripRequest:
    origin: str
    destination: str
    days: int
    budget_amount: Decimal
    currency: str
    budget_mode: BudgetMode
    expense_categories: tuple[str, ...]
    interests: tuple[str, ...]
    preferred_transport: str
    backup_transport: str | None = None
    housing_notes: str | None = None

    @property
    def traveler_count(self) -> int:
        return TRAVELER_COUNT

    def budget_limit(self) -> Decimal:
        """Потолок трат по правилу, которое ввёл пользователь."""
        if self.budget_mode is BudgetMode.PER_DAY:
            return self.budget_amount * self.days * self.traveler_count
        return self.budget_amount


def parse_city(raw: str, what: str) -> str:
    value = " ".join(raw.split())
    if not value:
        raise ValidationError(f"Введите {what}.")
    return value


def ensure_different_cities(origin: str, destination: str) -> None:
    if origin.casefold() == destination.casefold():
        raise ValidationError(
            "Город назначения должен отличаться от города отправления."
        )


def parse_days(raw: str) -> int:
    text = raw.strip()
    if not text.isdigit():
        raise ValidationError("Введите целое число дней больше нуля, без слов и дробей.")
    days = int(text)
    if days < 1:
        raise ValidationError("Длительность должна быть хотя бы 1 день.")
    return days


def parse_budget_amount(raw: str) -> Decimal:
    text = raw.strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValidationError("Введите сумму числом больше нуля.") from None
    if not amount.is_finite() or amount <= 0:
        raise ValidationError("Сумма бюджета должна быть больше нуля.")
    return amount


def parse_currency(raw: str) -> str:
    value = " ".join(raw.split())
    if not value:
        raise ValidationError("Введите валюту.")
    return value


def parse_budget_mode(raw: str) -> BudgetMode:
    key = raw.strip().lower().replace("ё", "е")
    if key in _PER_DAY_ANSWERS:
        return BudgetMode.PER_DAY
    if key in _WHOLE_TRIP_ANSWERS:
        return BudgetMode.WHOLE_TRIP
    raise ValidationError(
        "Выберите «1» — бюджет на день или «2» — бюджет на всю поездку."
    )


def parse_yes_no(raw: str) -> bool:
    key = raw.strip().lower().replace("ё", "е")
    if key in _YES_ANSWERS:
        return True
    if key in _NO_ANSWERS:
        return False
    raise ValidationError("Ответьте «да» или «нет».")


def parse_expense_selection(selected_ids: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    known = {key for key, _label in EXPENSE_CATEGORIES}
    unknown = [item for item in selected_ids if item not in known]
    if unknown:
        raise ValidationError("Неизвестная категория расходов.")
    selected = tuple(key for key, _label in EXPENSE_CATEGORIES if key in set(selected_ids))
    if not selected:
        raise ValidationError(
            "Отметьте хотя бы одну категорию, которая входит в бюджет."
        )
    return selected


def parse_interests(raw: str) -> tuple[str, ...]:
    items = tuple(" ".join(part.split()) for part in raw.split(",") if part.strip())
    if not items:
        raise ValidationError("Введите хотя бы один интерес.")
    return items


def _normalize_texts(
    items: tuple[str, ...] | list[str], empty_message: str
) -> tuple[str, ...]:
    cleaned = tuple(" ".join(item.split()) for item in items)
    if not cleaned or any(not item for item in cleaned):
        raise ValidationError(empty_message)
    return cleaned


def parse_required_text(raw: str, empty_message: str) -> str:
    value = " ".join(raw.split())
    if not value:
        raise ValidationError(empty_message)
    return value


def parse_optional_text(raw: str) -> str | None:
    value = " ".join(raw.split())
    return value or None


def create_trip(
    origin: str,
    destination: str,
    days: int,
    budget_amount: Decimal,
    currency: str,
    budget_mode: BudgetMode,
    expense_categories: tuple[str, ...] | list[str],
    interests: tuple[str, ...] | list[str],
    preferred_transport: str,
    backup_transport: str | None = None,
    housing_notes: str | None = None,
) -> TripRequest:
    origin = parse_city(origin, "город отправления")
    destination = parse_city(destination, "город назначения")
    ensure_different_cities(origin, destination)
    if type(days) is not int or days < 1:
        raise ValidationError("Длительность должна быть целым числом дней больше нуля.")
    if not isinstance(budget_amount, Decimal) or not budget_amount.is_finite() or budget_amount <= 0:
        raise ValidationError("Сумма бюджета должна быть больше нуля.")
    currency = parse_currency(currency)
    if not isinstance(budget_mode, BudgetMode):
        raise ValidationError("Укажите, бюджет задан на день или на всю поездку.")
    categories = parse_expense_selection(tuple(expense_categories))
    cleaned_interests = _normalize_texts(
        interests, "Введите хотя бы один интерес."
    )
    transport = parse_required_text(preferred_transport, "Введите предпочтительный транспорт.")
    backup = parse_optional_text(backup_transport or "")
    housing = parse_optional_text(housing_notes or "")
    return TripRequest(
        origin=origin,
        destination=destination,
        days=days,
        budget_amount=budget_amount,
        currency=currency,
        budget_mode=budget_mode,
        expense_categories=categories,
        interests=cleaned_interests,
        preferred_transport=transport,
        backup_transport=backup,
        housing_notes=housing,
    )


def category_label(category_id: str) -> str:
    for key, label in EXPENSE_CATEGORIES:
        if key == category_id:
            return label
    raise ValidationError("Неизвестная категория расходов.")


def budget_mode_label(mode: BudgetMode) -> str:
    if mode is BudgetMode.PER_DAY:
        return "на день"
    return "на всю поездку"


def format_amount(amount: Decimal) -> str:
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_trip(trip: TripRequest) -> str:
    included = [category_label(key) for key in trip.expense_categories]
    excluded = [
        label for key, label in EXPENSE_CATEGORIES if key not in trip.expense_categories
    ]
    lines = [
        "Поездка одного человека",
        f"Откуда: {trip.origin}",
        f"Куда: {trip.destination}",
        f"Дней: {trip.days}",
        (
            f"Бюджет: {format_amount(trip.budget_amount)} {trip.currency} "
            f"{budget_mode_label(trip.budget_mode)}"
        ),
        f"Потолок на поездку: {format_amount(trip.budget_limit())} {trip.currency}",
        f"В бюджет входит: {', '.join(included)}",
        f"Вне указанного бюджета: {', '.join(excluded) if excluded else 'нет'}",
        f"Интересы: {', '.join(trip.interests)}",
        f"Транспорт: {trip.preferred_transport}",
        f"Запасной транспорт: {trip.backup_transport or 'не указан'}",
        f"Пожелания по жилью: {trip.housing_notes or 'не указаны'}",
    ]
    return "\n".join(lines)
