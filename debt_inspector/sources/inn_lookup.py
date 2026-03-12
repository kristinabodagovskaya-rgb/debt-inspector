"""
Поиск названия организации по ИНН через egrul.nalog.ru.
"""

import re
import httpx

_cache: dict[str, str] = {}


async def lookup_inn(inn: str) -> str:
    """Пробивает ИНН через ЕГРЮЛ и возвращает название организации."""
    if inn in _cache:
        return _cache[inn]

    import asyncio
    # nalog.ru блокирует иностранные IP — ходим напрямую, без прокси
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            # Шаг 1: отправить запрос
            resp = await client.post("https://egrul.nalog.ru/", data={"query": inn})
            data = resp.json()
            token = data.get("t", "")
            if not token:
                _cache[inn] = ""
                return ""

            # Шаг 2: получить результат (с небольшой паузой)
            await asyncio.sleep(0.5)
            resp2 = await client.get(f"https://egrul.nalog.ru/search-result/{token}")
            result = resp2.json()

            rows = result.get("rows", [])
            if rows:
                name = rows[0].get("n", "") or rows[0].get("c", "")
                _cache[inn] = name
                return name

    except Exception:
        pass

    _cache[inn] = ""
    return ""


def is_pure_inn(value: str | None) -> bool:
    """Проверяет, является ли строка чистым ИНН (10 или 12 цифр)."""
    if not value:
        return False
    return bool(re.fullmatch(r"\d{10}|\d{12}", value.strip()))
