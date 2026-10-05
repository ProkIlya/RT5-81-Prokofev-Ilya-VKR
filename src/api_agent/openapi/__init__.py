"""Закрытый профиль OpenAPI: недоверенный документ не определяет сетевые права.

Описание профиля находится в docs/openapi-profile.md. Неизвестная конструкция
отклоняется целиком: отсутствие проверки не должно выглядеть как valid schema.
Снимок содержит только поддержанные декларации, без свободного текста/примеров.
"""

from .parser import Catalog, OpenApiError, parse_document
from .source import load_source

__all__ = ["Catalog", "OpenApiError", "parse_document", "load_source"]
