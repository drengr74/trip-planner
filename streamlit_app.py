"""Запуск веб-интерфейса из корня проекта.

Локально и как main file на Streamlit Community Cloud:
    streamlit run streamlit_app.py
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from trip_planner.streamlit_app import main

main()
