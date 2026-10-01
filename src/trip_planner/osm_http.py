"""HTTPS-запросы OSM с доверенными сертификатами certifi.

На части установок Python на macOS системный store пустой, и urllib
падает с CERTIFICATE_VERIFY_FAILED. certifi даёт актуальный CA-набор.
"""

from __future__ import annotations

import ssl
import urllib.request
from typing import Any

import certifi


def urlopen(request: urllib.request.Request, timeout: float) -> Any:
    context = ssl.create_default_context(cafile=certifi.where())
    return urllib.request.urlopen(request, timeout=timeout, context=context)
