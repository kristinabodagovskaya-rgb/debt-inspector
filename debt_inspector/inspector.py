"""
Главный оркестратор — запускает все источники параллельно, собирает отчёт.
"""

import asyncio
from datetime import datetime

from debt_inspector.models.debtor import SearchParams, DebtorInfo
from debt_inspector.models.report import DebtReport
from debt_inspector.sources.fssp import FSSPSource, CaptchaRequired
from debt_inspector.sources.efrsb import EFRSBSource
from debt_inspector.sources.kad_arbitr import KadArbitrSource
from debt_inspector.processing.deduplicator import (
    deduplicate_enforcements,
    deduplicate_court_cases,
    deduplicate_bankruptcies,
)
from debt_inspector.storage.cache import ResultCache


async def inspect(params: SearchParams, use_cache: bool = True) -> DebtReport | CaptchaRequired:
    """
    Запускает полную проверку по всем источникам.
    Может вернуть CaptchaRequired — тогда нужно показать капчу пользователю.
    """
    report = DebtReport(
        search_params=params,
        checked_at=datetime.now(),
    )

    captcha_exc = None

    # Параллельный запуск всех источников
    results = await asyncio.gather(
        _search_source(FSSPSource(), params, "ФССП"),
        _search_source(KadArbitrSource(), params, "КАД Арбитр"),
        _search_source(EFRSBSource(), params, "ЕФРСБ"),
        return_exceptions=True,
    )

    # ФССП
    if isinstance(results[0], CaptchaRequired):
        captcha_exc = results[0]
        report.errors.append("ФССП: требуется ввод капчи")
    elif isinstance(results[0], list):
        report.enforcements = results[0]
    elif isinstance(results[0], Exception):
        report.errors.append(f"ФССП: {results[0]}")

    # КАД Арбитр
    if isinstance(results[1], list):
        report.court_cases = results[1]
    elif isinstance(results[1], Exception):
        report.errors.append(f"КАД Арбитр: {results[1]}")

    # ЕФРСБ
    if isinstance(results[2], list):
        report.bankruptcies = results[2]
    elif isinstance(results[2], Exception):
        report.errors.append(f"ЕФРСБ: {results[2]}")

    # Если ФССП требует капчу — возвращаем её вместе с частичным отчётом
    if captcha_exc is not None:
        captcha_exc.partial_report = report
        return captcha_exc

    return _finalize_report(report, use_cache)


async def inspect_fssp_with_captcha(
    query_params: dict, code_id: str, captcha_code: str, partial_report: DebtReport | None = None
) -> DebtReport | CaptchaRequired:
    """Завершает поиск ФССП с введённой капчей."""
    source = FSSPSource()
    try:
        async with source:
            enforcements = await source.search_with_captcha(query_params, code_id, captcha_code)
    except CaptchaRequired as e:
        e.partial_report = partial_report
        return e
    except Exception as e:
        if partial_report:
            partial_report.errors.append(f"ФССП: {e}")
            return _finalize_report(partial_report, True)
        raise

    if partial_report:
        partial_report.enforcements = enforcements
        partial_report.errors = [e for e in partial_report.errors if "капча" not in e.lower()]
        return _finalize_report(partial_report, True)

    # Только ФССП
    report = DebtReport(
        search_params=SearchParams(),
        checked_at=datetime.now(),
        enforcements=enforcements,
    )
    return _finalize_report(report, True)


def _finalize_report(report: DebtReport, use_cache: bool) -> DebtReport:
    """Дедупликация, сводка, кеш."""
    report.enforcements = deduplicate_enforcements(report.enforcements)
    report.court_cases = deduplicate_court_cases(report.court_cases)
    report.bankruptcies = deduplicate_bankruptcies(report.bankruptcies)

    report.debtor_info = DebtorInfo(
        search_params=report.search_params,
        total_debt=report.total_enforcement_debt,
        active_enforcements=sum(
            1 for e in report.enforcements if e.status.value == "active"
        ),
        court_cases_count=len(report.court_cases),
        is_bankrupt=report.has_active_bankruptcy,
    )

    if use_cache:
        try:
            cache = ResultCache()
            cache.save(report)
            cache.close()
        except Exception:
            pass

    return report


async def _search_source(source, params: SearchParams, name: str):
    """Обёртка для поиска. Пробрасывает CaptchaRequired."""
    try:
        async with source:
            return await source.search(params)
    except CaptchaRequired:
        raise
    except Exception as e:
        raise RuntimeError(f"{name}: {e}") from e
