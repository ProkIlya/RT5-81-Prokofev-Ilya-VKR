"""Запуск приложения из checkout: python main.py --help."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from api_agent.main import main

if __name__ == '__main__':
    raise SystemExit(main())
