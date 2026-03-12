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


def _fmt_money(value, decimals=2):
    """Форматирует число как деньги: 1 534 500.12"""
    if value is None:
        return "—"
    fmt = f"{{:,.{decimals}f}}"
    return fmt.format(float(value)).replace(",", " ")


templates.env.filters["money"] = _fmt_money
templates.env.filters["money0"] = lambda v: _fmt_money(v, 0)

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
        return _render_captcha(request, user, result, "search", None)

    report = result
    return await _render_results(request, user, report)


# Кеш результатов капчи — защита от двойной отправки
_captcha_results: dict[str, object] = {}


@app.post("/fssp-captcha", response_class=HTMLResponse)
async def fssp_captcha_submit(
    request: Request,
    code_id: str = Form(""),
    captcha_code: str = Form(""),
    search_params_b64: str = Form(""),
    return_to: str = Form("search"),
    partial_report_id: str = Form(""),
):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # Защита от двойной отправки — если этот code_id уже обработан, берём из кеша
    cache_key = f"{code_id}:{captcha_code}"
    if cache_key in _captcha_results:
        result = _captcha_results[cache_key]
        if isinstance(result, CaptchaRequired):
            return _render_captcha(request, user, result, return_to, "Неправильный код, попробуйте ещё раз")
        return await _render_results(request, user, result)

    import json as _json
    import base64

    try:
        query_params = _json.loads(base64.b64decode(search_params_b64).decode())
    except Exception:
        query_params = {}

    # Получаем частичный отчёт
    partial = None
    entry = _reports.get(partial_report_id)
    if entry:
        _, partial = entry

    result = await inspect_fssp_with_captcha(query_params, code_id, captcha_code, partial)

    # Кешируем результат
    _captcha_results[cache_key] = result
    # Очистка старых (больше 20 записей)
    if len(_captcha_results) > 20:
        keys = list(_captcha_results.keys())
        for k in keys[:10]:
            del _captcha_results[k]

    if isinstance(result, CaptchaRequired):
        return _render_captcha(request, user, result, return_to, "Неправильный код, попробуйте ещё раз")

    return await _render_results(request, user, result)


def _render_captcha(request, user, captcha_exc: CaptchaRequired, return_to: str, error: str | None):
    """Рендерит страницу капчи."""
    import json as _json
    import base64

    params_b64 = base64.b64encode(
        _json.dumps(captcha_exc.query_params, ensure_ascii=False).encode()
    ).decode()

    return templates.TemplateResponse(
        "captcha.html",
        {
            "request": request,
            "user": user,
            "captcha_image": captcha_exc.captcha_image,
            "code_id": captcha_exc.code_id,
            "search_params_b64": params_b64,
            "return_to": return_to,
            "error": error,
            "partial_report_id": _save_partial(captcha_exc),
        },
    )


def _save_partial(captcha_exc: CaptchaRequired) -> str:
    """Сохраняет частичный отчёт для использования после капчи."""
    partial = getattr(captcha_exc, "partial_report", None)
    if partial:
        _cleanup_reports()
        pid = uuid.uuid4().hex[:12]
        _reports[pid] = (time.time(), partial)
        return pid
    return ""


async def _render_results(request, user, report):
    """Рендерит страницу результатов."""
    # Пробиваем ИНН взыскателей через ЕГРЮЛ
    from debt_inspector.sources.inn_lookup import lookup_inn, is_pure_inn
    for e in report.enforcements:
        if is_pure_inn(e.claimant):
            name = await lookup_inn(e.claimant.strip())
            if name:
                e.claimant = f"{name} (ИНН {e.claimant.strip()})"

    # Загружаем ранее сохранённые ручные долги из БД
    from debt_inspector.storage.cache import ResultCache
    from debt_inspector.models.enforcement import EnforcementProceeding, EnforcementStatus
    try:
        cache = ResultCache()
        key = cache.make_key(report)
        saved_debts = cache.get_manual_debts(key)
        cache.close()

        # Добавляем только те, которых ещё нет в отчёте
        existing = {(e.subject, e.amount) for e in report.enforcements}
        for d in saved_debts:
            subj = f"{d['description']} (ФНС, ручной ввод)"
            if (subj, d["amount"]) not in existing:
                report.enforcements.append(EnforcementProceeding(
                    subject=subj,
                    amount=d["amount"],
                    status=EnforcementStatus.ACTIVE,
                    claimant=d.get("claimant", "ФНС России"),
                ))
    except Exception:
        pass

    _cleanup_reports()
    report_id = uuid.uuid4().hex[:12]
    _reports[report_id] = (time.time(), report)

    # ФССП долг (без ФНС)
    fssp_debt = sum(e.amount or 0 for e in report.enforcements if not (e.subject and "ФНС" in e.subject))
    # ФНС долг
    fns_debt = sum(e.amount or 0 for e in report.enforcements if e.subject and "ФНС" in e.subject)
    # Общий долг
    total_all = fssp_debt + fns_debt

    # Определяем суд по региону
    region_name = ""
    if report.search_params.region:
        region_name = REGIONS.get(report.search_params.region, "")
    court_name = determine_court(region_name) if region_name else "Арбитражный суд по месту жительства"

    return templates.TemplateResponse(
        "results.html",
        {
            "request": request,
            "user": user,
            "report": report,
            "report_id": report_id,
            "total_enforcement_debt": fssp_debt,
            "total_court_claims": report.total_court_claims,
            "has_active_bankruptcy": report.has_active_bankruptcy,
            "total_all_debt": total_all,
            "court_name": court_name,
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


@app.post("/add-debt/{report_id}")
async def add_manual_debt(
    request: Request,
    report_id: str,
    description: str = Form(""),
    amount: float = Form(0),
):
    """Ручное добавление долга (ФНС и др.)."""
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    entry = _reports.get(report_id)
    if not entry:
        return HTMLResponse("<h3>Отчёт не найден или истёк</h3>", status_code=404)

    if not description or amount <= 0:
        # Возвращаем ту же страницу результатов
        _, report = entry
        return await _render_results(request, user, report)

    _, report = entry
    from debt_inspector.models.enforcement import EnforcementProceeding, EnforcementStatus
    report.enforcements.append(EnforcementProceeding(
        subject=f"{description} (ФНС, ручной ввод)",
        amount=amount,
        status=EnforcementStatus.ACTIVE,
        claimant="ФНС России",
    ))

    # Сохраняем в БД
    try:
        from debt_inspector.storage.cache import ResultCache
        cache = ResultCache()
        key = cache.make_key(report)
        cache.save_manual_debt(key, description, amount)
        cache.close()
    except Exception:
        pass

    # Обновляем отчёт в хранилище (тот же report_id)
    _reports[report_id] = (time.time(), report)

    return await _render_results(request, user, report)


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


def _start_proxy():
    """Поднимает SSH SOCKS-туннель если задан PROXY_SSH."""
    import subprocess
    ssh_target = os.getenv("PROXY_SSH")  # например: root@136.243.71.213:4022
    if not ssh_target or os.getenv("PROXY_URL"):
        return  # Уже задан готовый прокси или SSH не нужен

    try:
        host, _, port = ssh_target.rpartition(":")
        port = port or "22"
        subprocess.Popen(
            ["ssh", "-D", "1080", "-N", "-o", "StrictHostKeyChecking=no",
             "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=10",
             "-p", port, host],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.environ["PROXY_URL"] = "socks5://127.0.0.1:1080"
        print(f"  SOCKS прокси: localhost:1080 → {host}")
    except Exception as e:
        print(f"  Прокси не запущен: {e}")


def run():
    """Точка входа для debt-inspector-web."""
    import uvicorn

    _start_proxy()

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    print(f"\n  Debt Inspector Web: http://localhost:{port}\n")
    print(f"  Логин: inspector / debt2026\n")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run()
