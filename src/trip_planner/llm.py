"""Одна модель CrewAI для OpenAI-совместимого адреса ProxyAPI."""

from crewai import LLM

from .config import Settings, load_settings


def create_llm(settings: Settings | None = None) -> LLM:
    """Собирает модель. Запрос к API при этом не отправляет.

    Так CrewAI описывает свой OpenAI-совместимый адрес:
    флаг custom_openai, base_url и ключ. Имя модели берётся из настроек.
    """
    resolved = load_settings() if settings is None else settings
    return LLM(
        model=resolved.model,
        api_key=resolved.api_key,
        base_url=resolved.base_url,
        custom_openai=True,
    )
