"""Чтение настроек ProxyAPI и OSM из окружения, .env и плоских секретов."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Корень проекта: src/trip_planner/config.py → на два уровня выше.
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# Рекомендуемый адрес ProxyAPI для OpenAI-совместимого клиента.
PROXYAPI_BASE_URL = "https://api.proxyapi.ru/v1"

# Публичный Nominatim; для нагрузки лучше свой инстанс.
NOMINATIM_BASE_URL = "https://nominatim.openstreetmap.org"

# Публичный Overpass; для нагрузки лучше свой инстанс.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Публичный OSRM (учебный demo); для нагрузки лучше свой сервер.
OSRM_BASE_URL = "https://router.project-osrm.org"

# Радиус поиска достопримечательностей вокруг центра города, метры.
DEFAULT_OSM_SEARCH_RADIUS_M = 30_000


class ConfigError(Exception):
    """В окружении не хватает настройки для подключения."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str

    def __repr__(self) -> str:
        return (
            f"Settings(base_url={self.base_url!r}, model={self.model!r}, "
            "api_key='[скрыто]')"
        )


@dataclass(frozen=True)
class OsmSettings:
    user_agent: str
    nominatim_url: str
    overpass_url: str
    osrm_url: str
    search_radius_m: int

    def __repr__(self) -> str:
        return (
            f"OsmSettings(nominatim_url={self.nominatim_url!r}, "
            f"overpass_url={self.overpass_url!r}, "
            f"osrm_url={self.osrm_url!r}, "
            f"search_radius_m={self.search_radius_m}, "
            f"user_agent={self.user_agent!r})"
        )


def _load_env() -> None:
    load_dotenv(_ENV_FILE)


def _config_value(name: str) -> str:
    """Сначала окружение и .env, затем плоский ключ st.secrets.

    Значение секрета не печатается. Если секреты Streamlit недоступны
    (консоль без secrets.toml), возвращает пустую строку.
    """
    from_env = os.environ.get(name, "").strip()
    if from_env:
        return from_env
    return _flat_secret(name)


def _flat_secret(name: str) -> str:
    try:
        import streamlit as st
        from streamlit.errors import StreamlitSecretNotFoundError
    except ImportError:
        return ""
    try:
        raw = st.secrets[name]
    except (StreamlitSecretNotFoundError, KeyError, AttributeError, FileNotFoundError):
        return ""
    if isinstance(raw, bool) or isinstance(raw, Mapping):
        return ""
    if isinstance(raw, (str, int, float)):
        return str(raw).strip()
    return ""


def load_settings() -> Settings:
    """Читает ключ, адрес и модель. Ключ в сообщении об ошибке не показывает."""
    _load_env()
    api_key = _config_value("OPENAI_API_KEY")
    base_url = _config_value("OPENAI_BASE_URL") or PROXYAPI_BASE_URL
    model = _config_value("OPENAI_MODEL")
    if not api_key:
        raise ConfigError(
            "Задайте OPENAI_API_KEY в .env, в окружении или плоским секретом Streamlit."
        )
    if not model:
        raise ConfigError(
            "Задайте OPENAI_MODEL в .env, в окружении или плоским секретом Streamlit. "
            "Идентификатор выглядит как «вендор/модель»."
        )
    if "/" not in model:
        raise ConfigError(
            "OPENAI_MODEL должен быть в виде «вендор/модель», как в каталоге ProxyAPI."
        )
    return Settings(api_key=api_key, base_url=base_url, model=model)


def load_osm_settings() -> OsmSettings:
    """Читает User-Agent и адреса Nominatim/Overpass/OSRM."""
    _load_env()
    user_agent = _config_value("OSM_USER_AGENT")
    nominatim_url = (
        _config_value("NOMINATIM_URL") or NOMINATIM_BASE_URL
    ).rstrip("/")
    overpass_url = (_config_value("OVERPASS_URL") or OVERPASS_URL).rstrip("/")
    osrm_url = (_config_value("OSRM_URL") or OSRM_BASE_URL).rstrip("/")
    radius_raw = _config_value("OSM_SEARCH_RADIUS_M")
    if radius_raw:
        try:
            search_radius_m = int(radius_raw)
        except ValueError as error:
            raise ConfigError(
                "OSM_SEARCH_RADIUS_M должен быть целым числом метров."
            ) from error
    else:
        search_radius_m = DEFAULT_OSM_SEARCH_RADIUS_M
    if search_radius_m < 1:
        raise ConfigError("OSM_SEARCH_RADIUS_M должен быть не меньше 1 метра.")
    if not user_agent:
        raise ConfigError(
            "Задайте OSM_USER_AGENT в .env, в окружении или плоским секретом Streamlit. "
            "Укажите название приложения и контакт, например: "
            "trip-planner/0.1 (you@example.com)."
        )
    if "@" not in user_agent and "http" not in user_agent.lower():
        raise ConfigError(
            "OSM_USER_AGENT должен идентифицировать приложение и контакт "
            "(email или URL), как требует политика Nominatim."
        )
    return OsmSettings(
        user_agent=user_agent,
        nominatim_url=nominatim_url,
        overpass_url=overpass_url,
        osrm_url=osrm_url,
        search_radius_m=search_radius_m,
    )


def load_tavily_api_key() -> str:
    """Ключ Tavily, если он задан. Пустая строка значит, что веб-поиск недоступен.

    Значение не печатается и не попадает в сообщения об ошибках.
    """
    _load_env()
    return _config_value("TAVILY_API_KEY")
