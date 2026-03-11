"""
CLI интерфейс для агента-инспектора долгов.

Использование:
  debt-inspector person --last-name Иванов --first-name Иван --region 77
  debt-inspector company --name "ООО Рога и Копыта" --inn 7707083893
  debt-inspector inn 770708389312
  debt-inspector history --inn 770708389312
"""

import asyncio
from pathlib import Path
from datetime import datetime

import typer
from rich.console import Console

from debt_inspector.models.debtor import SearchParams, SubjectType
from debt_inspector.inspector import inspect
from debt_inspector.processing.reporter import export_json, export_excel, print_report
from debt_inspector.storage.cache import ResultCache

app = typer.Typer(
    name="debt-inspector",
    help="Агент-инспектор по поиску долгов: ФССП, суды, ЕФРСБ",
    no_args_is_help=True,
)
console = Console()


@app.command()
def person(
    last_name: str = typer.Option(..., "--last-name", "-l", help="Фамилия"),
    first_name: str = typer.Option(None, "--first-name", "-f", help="Имя"),
    middle_name: str = typer.Option(None, "--middle-name", "-m", help="Отчество"),
    birth_date: str = typer.Option(None, "--birth-date", "-b", help="Дата рождения ДД.ММ.ГГГГ"),
    inn: str = typer.Option(None, "--inn", help="ИНН"),
    region: int = typer.Option(None, "--region", "-r", help="Код региона (77=Москва)"),
    output: str = typer.Option(None, "--output", "-o", help="Путь для сохранения (json/xlsx)"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Не сохранять в кеш"),
):
    """Поиск долгов физического лица."""
    params = SearchParams(
        subject_type=SubjectType.PERSON,
        last_name=last_name,
        first_name=first_name,
        middle_name=middle_name,
        birth_date=birth_date,
        inn=inn,
        region=region,
    )
    _run_inspection(params, output, not no_cache)


@app.command()
def company(
    name: str = typer.Option(None, "--name", "-n", help="Наименование организации"),
    inn: str = typer.Option(None, "--inn", help="ИНН"),
    ogrn: str = typer.Option(None, "--ogrn", help="ОГРН"),
    region: int = typer.Option(None, "--region", "-r", help="Код региона"),
    output: str = typer.Option(None, "--output", "-o", help="Путь для сохранения (json/xlsx)"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Не сохранять в кеш"),
):
    """Поиск долгов юридического лица."""
    if not name and not inn:
        console.print("[red]Укажите --name или --inn[/red]")
        raise typer.Exit(1)

    params = SearchParams(
        subject_type=SubjectType.COMPANY,
        company_name=name,
        inn=inn,
        ogrn=ogrn,
        region=region,
    )
    _run_inspection(params, output, not no_cache)


@app.command()
def inn(
    inn_number: str = typer.Argument(help="ИНН (10 или 12 цифр)"),
    region: int = typer.Option(None, "--region", "-r", help="Код региона"),
    output: str = typer.Option(None, "--output", "-o", help="Путь для сохранения"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Не сохранять в кеш"),
):
    """Быстрый поиск по ИНН (авто-определение: физлицо/юрлицо)."""
    subject_type = SubjectType.PERSON if len(inn_number) == 12 else SubjectType.COMPANY

    params = SearchParams(
        subject_type=subject_type,
        inn=inn_number,
        region=region,
    )
    _run_inspection(params, output, not no_cache)


@app.command()
def history(
    inn_val: str = typer.Option(None, "--inn", help="ИНН"),
    name: str = typer.Option(None, "--name", help="ФИО или наименование"),
    limit: int = typer.Option(10, "--limit", help="Кол-во записей"),
):
    """Показать историю проверок из кеша."""
    if not inn_val and not name:
        console.print("[red]Укажите --inn или --name[/red]")
        raise typer.Exit(1)

    key = f"inn:{inn_val}" if inn_val else f"name:{name}".lower().strip()
    cache = ResultCache()
    records = cache.get_history(key, limit)
    cache.close()

    if not records:
        console.print("[yellow]История проверок не найдена.[/yellow]")
        return

    from rich.table import Table

    table = Table(title=f"История проверок: {inn_val or name}")
    table.add_column("Дата")
    table.add_column("ИП")
    table.add_column("Суды")
    table.add_column("Банкротства")
    table.add_column("Общий долг", justify="right")

    for r in records:
        table.add_row(
            r["checked_at"][:16],
            str(r["enforcements"]),
            str(r["court_cases"]),
            str(r["bankruptcies"]),
            f"{r['total_debt']:,.2f} руб.",
        )

    console.print(table)


def _run_inspection(params: SearchParams, output: str | None, use_cache: bool):
    """Запуск проверки и вывод результатов."""
    console.print(f"\n[bold]Проверка: {params.display_name}[/bold]\n")

    report = asyncio.run(inspect(params, use_cache=use_cache))

    # Вывод в консоль
    print_report(report)

    # Экспорт
    if output:
        path = Path(output)
        if path.suffix == ".xlsx":
            result_path = export_excel(report, path)
        else:
            if not path.suffix:
                path = path.with_suffix(".json")
            result_path = export_json(report, path)
        console.print(f"[green]Отчёт сохранён: {result_path}[/green]")
    else:
        # Автосохранение в reports/
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name_slug = params.display_name.replace(" ", "_")[:30]
        reports_dir = Path("reports")
        json_path = export_json(report, reports_dir / f"{name_slug}_{ts}.json")
        xlsx_path = export_excel(report, reports_dir / f"{name_slug}_{ts}.xlsx")
        console.print(f"\n[dim]Автосохранение:[/dim]")
        console.print(f"  JSON: {json_path}")
        console.print(f"  Excel: {xlsx_path}")


if __name__ == "__main__":
    app()
