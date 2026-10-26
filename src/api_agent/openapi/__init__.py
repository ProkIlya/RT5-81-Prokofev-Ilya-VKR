"""Закрытый профиль OpenAPI: недоверенный документ не определяет сетевые права.

Описание профиля находится в docs/engineering/openapi-profile.md. Неизвестная конструкция
отклоняется целиком: отсутствие проверки не должно выглядеть как valid schema.
Снимок содержит только поддержанные декларации, без свободного текста/примеров.
"""

from api_agent.openapi.parser import Catalog, OpenApiError, parse_document
from api_agent.openapi.source import load_source

__all__ = ["Catalog", "OpenApiError", "parse_document", "load_source"]
