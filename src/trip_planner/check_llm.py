"""Короткая ручная проверка связи с моделью.

Запуск из корня проекта:
    PYTHONPATH=src python3 -m trip_planner.check_llm

При импорте запрос не отправляется.
"""

import sys

from .config import ConfigError, load_settings
from .llm import create_llm


def main() -> None:
    settings = None
    try:
        settings = load_settings()
        answer = create_llm(settings).call("Ответь одним словом: ок")
    except ConfigError as error:
        print(error.message)
        sys.exit(1)
    except Exception as error:
        secret = settings.api_key if settings is not None else ""
        print(_safe_message(error, secret))
        sys.exit(1)
    text = str(answer).strip()
    print(f"Ответ модели {settings.model}: {text or 'пусто'}")


def _safe_message(error: Exception, secret: str) -> str:
    text = str(error)
    if secret:
        text = text.replace(secret, "[скрыто]")
    return f"Не удалось связаться с моделью: {text}"


if __name__ == "__main__":
    main()
