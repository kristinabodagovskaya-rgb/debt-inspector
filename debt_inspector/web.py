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
from debt_inspector.models.bankruptcy_case_info import (
    DebtorProfile,
    PropertyItem,
    PropertyType,
    IncomeInfo,
    determine_route,
    determine_court,
    ARBITRATION_COURTS,
)
from debt_inspector.inspector import inspect, inspect_fssp_with_captcha
from debt_inspector.sources.fssp import CaptchaRequired
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

    result = await inspect(params, use_cache=True)

    # Капча ФССП — показываем пользователю
    if isinstance(result, CaptchaRequired):
        import json as _json
        return templates.TemplateResponse(
            "captcha.html",
            {
                "request": request,
                "user": user,
                "captcha_image": result.captcha_image,
                "code_id": result.code_id,
                "search_params_json": _json.dumps(result.query_params, ensure_ascii=False),
                "return_to": "search",
                "error": None,
                "partial_report_id": _save_partial(result),
            },
        )

    report = result
    return _render_results(request, user, report)


@app.post("/fssp-captcha", response_class=HTMLResponse)
async def fssp_captcha_submit(
    request: Request,
    code_id: str = Form(""),
    captcha_code: str = Form(""),
    search_params: str = Form("{}"),
    return_to: str = Form("search"),
    partial_report_id: str = Form(""),
):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    import json as _json
    query_params = _json.loads(search_params)

    # Получаем частичный отчёт
    partial = None
    entry = _reports.get(partial_report_id)
    if entry:
        _, partial = entry

    result = await inspect_fssp_with_captcha(query_params, code_id, captcha_code, partial)

    if isinstance(result, CaptchaRequired):
        return templates.TemplateResponse(
            "captcha.html",
            {
                "request": request,
                "user": user,
                "captcha_image": result.captcha_image,
                "code_id": result.code_id,
                "search_params_json": _json.dumps(result.query_params, ensure_ascii=False),
                "return_to": return_to,
                "error": "Неправильный код, попробуйте ещё раз",
                "partial_report_id": _save_partial(result),
            },
        )

    return _render_results(request, user, result)


def _save_partial(captcha_exc: CaptchaRequired) -> str:
    """Сохраняет частичный отчёт для использования после капчи."""
    partial = getattr(captcha_exc, "partial_report", None)
    if partial:
        _cleanup_reports()
        pid = uuid.uuid4().hex[:12]
        _reports[pid] = (time.time(), partial)
        return pid
    return ""


def _render_results(request, user, report):
    """Рендерит страницу результатов."""
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


@app.get("/bankruptcy", response_class=HTMLResponse)
async def bankruptcy_form(request: Request):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    regions = sorted(ARBITRATION_COURTS.keys())
    return templates.TemplateResponse(
        "bankruptcy_form.html",
        {"request": request, "user": user, "regions": regions},
    )


@app.post("/bankruptcy", response_class=HTMLResponse)
async def bankruptcy_submit(request: Request):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    form = await request.form()

    # Собираем профиль
    profile = DebtorProfile(
        last_name=form.get("last_name", ""),
        first_name=form.get("first_name", ""),
        middle_name=form.get("middle_name", ""),
        birth_date=form.get("birth_date", ""),
        inn=form.get("inn", ""),
        snils=form.get("snils", ""),
        region=form.get("region", ""),
        city=form.get("city", ""),
        address=form.get("address", ""),
        phone=form.get("phone", ""),
        email=form.get("email", ""),
        total_debt=float(form.get("total_debt", 0) or 0),
        debt_details=form.get("debt_details", ""),
        income=IncomeInfo(
            employment_type=form.get("employment_type", ""),
            monthly_income=float(form.get("monthly_income", 0) or 0),
            employer=form.get("employer", ""),
            dependents_count=int(form.get("dependents_count", 0) or 0),
        ),
    )

    # Имущество (динамические поля)
    for i in range(20):
        prop_type = form.get(f"prop_type_{i}", "")
        if not prop_type:
            continue
        profile.properties.append(PropertyItem(
            property_type=PropertyType(prop_type) if prop_type in PropertyType.__members__.values() else PropertyType.OTHER,
            description=form.get(f"prop_desc_{i}", ""),
            estimated_value=float(form.get(f"prop_value_{i}", 0) or 0),
            is_sole_housing=bool(form.get(f"prop_sole_{i}", "")),
        ))

    # Определяем маршрут и суд
    profile.bankruptcy_route = determine_route(profile.total_debt)
    profile.jurisdiction_court = determine_court(profile.region)

    # Запуск автопоиска долгов
    report = None
    report_id = None
    total_enforcement_debt = 0

    if profile.last_name:
        # Конвертация даты
        formatted_date = ""
        if profile.birth_date:
            try:
                parts = profile.birth_date.split("-")
                formatted_date = f"{parts[2]}.{parts[1]}.{parts[0]}"
            except (IndexError, ValueError):
                formatted_date = profile.birth_date

        # Определяем код региона
        region_code = None
        for code, name in REGIONS.items():
            if name.lower() in profile.region.lower() or profile.region.lower() in name.lower():
                region_code = code
                break

        params = SearchParams(
            subject_type=SubjectType.PERSON,
            last_name=profile.last_name or None,
            first_name=profile.first_name or None,
            middle_name=profile.middle_name or None,
            birth_date=formatted_date or None,
            inn=profile.inn or None,
            region=region_code,
        )

        try:
            report = await inspect(params, use_cache=True)
            total_enforcement_debt = report.total_enforcement_debt

            _cleanup_reports()
            report_id = uuid.uuid4().hex[:12]
            _reports[report_id] = (time.time(), report)
        except Exception:
            pass

    return templates.TemplateResponse(
        "bankruptcy_result.html",
        {
            "request": request,
            "user": user,
            "profile": profile,
            "route": profile.bankruptcy_route.value,
            "report": report,
            "report_id": report_id,
            "total_enforcement_debt": total_enforcement_debt,
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
