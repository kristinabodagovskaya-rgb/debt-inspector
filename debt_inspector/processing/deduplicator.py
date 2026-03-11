"""
Дедупликация результатов из разных источников.
"""

from debt_inspector.models.enforcement import EnforcementProceeding
from debt_inspector.models.court_case import CourtCase
from debt_inspector.models.bankruptcy import BankruptcyCase


def deduplicate_enforcements(
    items: list[EnforcementProceeding],
) -> list[EnforcementProceeding]:
    """Убирает дубли ИП по номеру."""
    seen = {}
    for item in items:
        key = item.number or id(item)
        if key not in seen:
            seen[key] = item
        else:
            # Оставляем запись с большим количеством данных
            existing = seen[key]
            if _score(item) > _score(existing):
                seen[key] = item
    return list(seen.values())


def deduplicate_court_cases(items: list[CourtCase]) -> list[CourtCase]:
    """Убирает дубли судебных дел по номеру."""
    seen = {}
    for item in items:
        key = item.case_number or id(item)
        if key not in seen:
            seen[key] = item
        else:
            existing = seen[key]
            if _score(item) > _score(existing):
                seen[key] = item
    return list(seen.values())


def deduplicate_bankruptcies(items: list[BankruptcyCase]) -> list[BankruptcyCase]:
    """Убирает дубли банкротств по ИНН + номеру дела."""
    seen = {}
    for item in items:
        key = (item.debtor_inn or "", item.case_number or str(id(item)))
        if key not in seen:
            seen[key] = item
        else:
            existing = seen[key]
            if _score(item) > _score(existing):
                seen[key] = item
    return list(seen.values())


def _score(obj) -> int:
    """Считает количество заполненных полей — чем больше, тем лучше."""
    count = 0
    for field_name in obj.model_fields:
        val = getattr(obj, field_name, None)
        if val is not None and val != "" and val != 0:
            count += 1
    return count
