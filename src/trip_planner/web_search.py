"""Один базовый запрос Tavily. Тема интереса здесь не разбирается.

Клиент не вызывает CrewAI и не обращается к Streamlit. Ключ в ответ и в
текст ошибки не попадает. Повторов и платного режима нет.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import load_tavily_api_key
from .osm_http import urlopen

TAVILY_SEARCH_URL = "https://api.tavily.com/search"

# Фразы для агента. Отсутствие места в ответе Overpass не означает, что его нет в OSM.
OSM_INTEREST_UNCONFIRMED = "OSM не подтвердил интерес среди найденных мест"
OSM_INTEREST_CONFIRMED = "подтверждено среди найденных мест"
WEB_EMPTY = "В веб-поиске ничего не найдено"
WEB_UNAVAILABLE = "Веб-поиск недоступен"
WEB_NOT_ON_TOPIC = "Веб-поиск не нашёл источников, связанных с темой и местом"
WEB_BLOCK_TITLE = "Веб, не данные OpenStreetMap"

_MAX_SOURCES = 3
_FETCH_CAP = 10
_SNIPPET_CHARS = 240
_TIMEOUT_SEC = 20.0


@dataclass(frozen=True)
class WebSource:
    title: str
    site: str
    url: str
    checked_on: str
    snippet: str


@dataclass(frozen=True)
class WebSearchOutcome:
    """Успешные источники, пустой ответ или недоступный поиск. Без ключа."""

    unavailable: bool
    sources: tuple[WebSource, ...] = ()


def search_web(query: str, *, max_results: int = _MAX_SOURCES) -> WebSearchOutcome:
    """Один запрос search_depth=basic. Любая ошибка — поиск недоступен, без повтора.

    Тема интереса и география здесь не разбираются: вызывающий сам отбирает
    уже полученные источники.
    """
    text = " ".join(query.split())
    api_key = load_tavily_api_key()
    if not text or not api_key:
        return WebSearchOutcome(unavailable=True)
    limit = _MAX_SOURCES
    if type(max_results) is int and max_results >= 1:
        limit = min(max_results, _FETCH_CAP)

    body = json.dumps(
        {
            "query": text,
            "search_depth": "basic",
            "max_results": limit,
            "include_answer": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TAVILY_SEARCH_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=_TIMEOUT_SEC) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        UnicodeError,
        OSError,
        ValueError,
    ):
        return WebSearchOutcome(unavailable=True)
    if not isinstance(payload, dict):
        return WebSearchOutcome(unavailable=True)
    results = payload.get("results")
    if not isinstance(results, list):
        return WebSearchOutcome(unavailable=True)

    checked_on = datetime.now(timezone.utc).date().isoformat()
    sources: list[WebSource] = []
    for item in results:
        if len(sources) >= limit:
            break
        source = _source_from_item(item, checked_on)
        if source is not None:
            sources.append(source)
    return WebSearchOutcome(unavailable=False, sources=tuple(sources))


def _source_from_item(item: object, checked_on: str) -> WebSource | None:
    if not isinstance(item, dict):
        return None
    url = str(item.get("url") or "").strip()
    site = _site_from_url(url)
    if not site:
        return None
    title = " ".join(str(item.get("title") or "").split()) or site
    raw_snippet = item.get("content")
    if raw_snippet is None:
        raw_snippet = item.get("snippet")
    return WebSource(
        title=title,
        site=site,
        url=url,
        checked_on=checked_on,
        snippet=_short_snippet(str(raw_snippet or "")),
    )


def _site_from_url(url: str) -> str:
    if not url.lower().startswith(("http://", "https://")):
        return ""
    host = urllib.parse.urlparse(url).hostname or ""
    if host.lower().startswith("www."):
        return host[4:]
    return host


def _short_snippet(text: str) -> str:
    snippet = " ".join(text.split())
    if len(snippet) <= _SNIPPET_CHARS:
        return snippet
    return snippet[:_SNIPPET_CHARS].rstrip()
