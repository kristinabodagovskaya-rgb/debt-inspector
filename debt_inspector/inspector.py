"""
Главный оркестратор — запускает все источники параллельно, собирает отчёт.
"""

import asyncio
from datetime import datetime

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from debt_inspector.models.debtor import SearchParams, DebtorInfo
from debt_inspector.models.report import DebtReport
from debt_inspector.sources.fssp import FSSPSource
from debt_inspector.sources.efrsb import EFRSBSource
from debt_inspector.sources.kad_arbitr import KadArbitrSource
from debt_inspector.processing.deduplicator import (
    deduplicate_enforcements,
    deduplicate_court_cases,
    deduplicate_bankruptcies,
)
from debt_inspector.storage.cache import ResultCache

console = Console()


async def inspect(params: SearchParams, use_cache: bool = True) -> DebtReport:
    """Запускает полную проверку по всем источникам."""

    report = DebtReport(
        search_params=params,
        checked_at=datetime.now(),
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task_fssp = progress.add_task("[cyan]ФССП — исполнительные производства...", total=None)
        task_kad = progress.add_task("[cyan]КАД Арбитр — арбитражные дела...", total=None)
        task_efrsb = progress.add_task("[cyan]ЕФРСБ — банкротства...", total=None)

        # Параллельный запуск всех источников
        results = await asyncio.gather(
            _search_source(FSSPSource(), params, "ФССП"),
            _search_source(KadArbitrSource(), params, "КАД Арбитр"),
            _search_source(EFRSBSource(), params, "ЕФРСБ"),
            return_exceptions=True,
        )

        # ФССП
        progress.update(task_fssp, completed=True)
        if isinstance(results[0], list):
            report.enforcements = results[0]
            progress.update(task_fssp, description=f"[green]ФССП: {len(results[0])} записей")
        elif isinstance(results[0], Exception):
            err = str(results[0])
            report.errors.append(err)
            progress.update(task_fssp, description=f"[red]ФССП: {err[:60]}")

        # КАД Арбитр
        progress.update(task_kad, completed=True)
        if isinstance(results[1], list):
            report.court_cases = results[1]
            progress.update(task_kad, description=f"[green]КАД Арбитр: {len(results[1])} дел")
        elif isinstance(results[1], Exception):
            err = str(results[1])
            report.errors.append(err)
            progress.update(task_kad, description=f"[red]КАД Арбитр: {err[:60]}")

        # ЕФРСБ
        progress.update(task_efrsb, completed=True)
        if isinstance(results[2], list):
            report.bankruptcies = results[2]
            progress.update(task_efrsb, description=f"[green]ЕФРСБ: {len(results[2])} записей")
        elif isinstance(results[2], Exception):
            err = str(results[2])
            report.errors.append(err)
            progress.update(task_efrsb, description=f"[red]ЕФРСБ: {err[:60]}")

    # Дедупликация
    report.enforcements = deduplicate_enforcements(report.enforcements)
    report.court_cases = deduplicate_court_cases(report.court_cases)
    report.bankruptcies = deduplicate_bankruptcies(report.bankruptcies)

    # Сводка
    report.debtor_info = DebtorInfo(
        search_params=params,
        total_debt=report.total_enforcement_debt,
        active_enforcements=sum(
            1 for e in report.enforcements if e.status.value == "active"
        ),
        court_cases_count=len(report.court_cases),
        is_bankrupt=report.has_active_bankruptcy,
    )

    # Кеш
    if use_cache:
        try:
            cache = ResultCache()
            cache.save(report)
            cache.close()
        except Exception:
            pass  # Кеш не критичен

    return report


async def _search_source(source, params: SearchParams, name: str) -> list:
    """Обёртка для поиска с graceful error handling."""
    try:
        async with source:
            return await source.search(params)
    except Exception as e:
        raise RuntimeError(f"{name}: {e}") from e
