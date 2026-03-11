"""
Веб-интерфейс Debt Inspector.

Запуск: debt-inspector-web
Или: python -m debt_inspector.web
"""

import os
import uuid
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from debt_inspector.auth import check_credentials
from debt_inspector.models.debtor import SearchParams, SubjectType
from debt_inspector.inspector import inspect
from debt_inspector.processing.reporter import export_json, export_excel

app = FastAPI(title="Debt Inspector")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", uuid.uuid4().hex),
)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# In-memory хранилище отчётов для скачивания (TTL 1 час)
_reports: dict[str, tuple[float, object]] = {}
REPORT_TTL = 3600

REGIONS = {
    77: "Москва",
    78: "Санкт-Петербург",
    50: "Московская область",
    47: "Ленинградская область",
    23: "Краснодарский край",
    52: "Нижегородская область",
    16: "Республика Татарстан",
    66: "Свердловская область",
    63: "Самарская область",
    61: "Ростовская область",
    74: "Челябинская область",
    2: "Республика Башкортостан",
    59: "Пермский край",
    34: "Волгоградская область",
    54: "Новосибирская область",
    24: "Красноярский край",
    42: "Кемеровская область",
    56: "Оренбургская область",
    26: "Ставропольский край",
    36: "Воронежская область",
}


def _cleanup_reports():
    """Удаляет устаревшие отчёты."""
    now = time.time()
    expired = [k for k, (ts, _) in _reports.items() if now - ts > REPORT_TTL]
    for k in expired:
        del _reports[k]


def _get_user(request: Request) -> str | None:
    return request.session.get("user")


# --- Маршруты ---


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if _get_user(request):
        return RedirectResponse("/search", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _get_user(request):
        return RedirectResponse("/search", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "user": None, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, login: str = Form(...), password: str = Form(...)):
    if check_credentials(login, password):
        request.session["user"] = login
        return RedirectResponse("/search", status_code=302)

    return templates.TemplateResponse(
        "login.html",
        {"request": request, "user": None, "error": "Неверный логин или пароль"},
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    return templates.TemplateResponse(
        "search.html",
        {"request": request, "user": user, "regions": REGIONS},
    )


@app.post("/search", response_class=HTMLResponse)
async def search_submit(
    request: Request,
    subject_type: str = Form("person"),
    last_name: str = Form(""),
    first_name: str = Form(""),
    middle_name: str = Form(""),
    birth_date: str = Form(""),
    inn: str = Form(""),
    inn_company: str = Form(""),
    company_name: str = Form(""),
    ogrn: str = Form(""),
    region: str = Form(""),
):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # Конвертация даты из YYYY-MM-DD в ДД.ММ.ГГГГ
    formatted_date = ""
    if birth_date:
        try:
            parts = birth_date.split("-")
            formatted_date = f"{parts[2]}.{parts[1]}.{parts[0]}"
        except (IndexError, ValueError):
            formatted_date = birth_date

    region_int = int(region) if region else None

    if subject_type == "company":
        params = SearchParams(
            subject_type=SubjectType.COMPANY,
            company_name=company_name or None,
            inn=inn_company or None,
            ogrn=ogrn or None,
            region=region_int,
        )
    else:
        params = SearchParams(
            subject_type=SubjectType.PERSON,
            last_name=last_name or None,
            first_name=first_name or None,
            middle_name=middle_name or None,
            birth_date=formatted_date or None,
            inn=inn or None,
            region=region_int,
        )

    report = await inspect(params, use_cache=True)

    # Сохраняем для скачивания
    _cleanup_reports()
    report_id = uuid.uuid4().hex[:12]
    _reports[report_id] = (time.time(), report)

    return templates.TemplateResponse(
        "results.html",
        {
            "request": request,
            "user": user,
            "report": report,
            "report_id": report_id,
            "total_enforcement_debt": report.total_enforcement_debt,
            "total_court_claims": report.total_court_claims,
            "has_active_bankruptcy": report.has_active_bankruptcy,
        },
    )


@app.get("/download/{report_id}")
async def download_report(request: Request, report_id: str, format: str = "json"):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    entry = _reports.get(report_id)
    if not entry:
        return HTMLResponse("<h3>Отчёт не найден или истёк</h3>", status_code=404)

    _, report = entry
    tmp_dir = Path(tempfile.mkdtemp())
    name = report.search_params.display_name.replace(" ", "_")[:20]

    if format == "xlsx":
        path = export_excel(report, tmp_dir / f"{name}.xlsx")
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        path = export_json(report, tmp_dir / f"{name}.json")
        media_type = "application/json"

    return FileResponse(path, media_type=media_type, filename=path.name)


def run():
    """Точка входа для debt-inspector-web."""
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    print(f"\n  Debt Inspector Web: http://localhost:{port}\n")
    print(f"  Логин: inspector / debt2026\n")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run()
