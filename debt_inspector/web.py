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
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
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
    check_mfc_eligibility,
    get_sro_for_region,
    get_court_details,
    ARBITRATION_COURTS,
    SRO_LIST,
)
from debt_inspector.models.pipeline import (
    BankruptcyPipeline,
    CreditorInfo,
    PipelineStep,
    PIPELINE_STEPS,
    get_documents_for_route,
    DocumentStatus,
)
from debt_inspector.models.bankruptcy_case_info import recommend_acceleration
from debt_inspector.processing.qr_payment import generate_fee_qr, generate_deposit_qr
from debt_inspector.inspector import inspect, inspect_fssp_with_captcha
from debt_inspector.sources.fssp import CaptchaRequired
from debt_inspector.processing.reporter import export_json, export_excel
from debt_inspector.storage.cache import ResultCache

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


# --- Pipeline банкротства ---


def _get_pipeline_cache():
    """Возвращает экземпляр ResultCache для pipeline."""
    return ResultCache()


def _load_pipeline(pid: str) -> BankruptcyPipeline | None:
    """Загружает pipeline из БД."""
    try:
        cache = _get_pipeline_cache()
        data = cache.get_pipeline(pid)
        cache.close()
        if data:
            return BankruptcyPipeline(**data["data"])
    except Exception:
        pass
    return None


def _save_pipeline_data(pid: str, pipeline: BankruptcyPipeline):
    """Сохраняет pipeline в БД."""
    try:
        cache = _get_pipeline_cache()
        cache.save_pipeline(pid, pipeline.current_step.value, pipeline.model_dump(mode="json"))
        cache.close()
    except Exception:
        pass


def _pipeline_context(pipeline: BankruptcyPipeline, pid: str, step_num: int) -> dict:
    """Базовый контекст для шаблонов pipeline."""
    return {
        "pipeline": pipeline,
        "pid": pid,
        "steps": PIPELINE_STEPS,
        "current_step_num": step_num,
    }


@app.get("/pipeline/start", response_class=HTMLResponse)
async def pipeline_start(request: Request, report_id: str = ""):
    """Начало pipeline — создаёт pipeline из report и редиректит на assessment."""
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    pid = uuid.uuid4().hex[:12]
    pipeline = BankruptcyPipeline(report_id=report_id, current_step=PipelineStep.ASSESSMENT)

    # Заполняем из отчёта
    entry = _reports.get(report_id)
    if entry:
        _, report = entry
        sp = report.search_params

        # ФИО
        pipeline.last_name = sp.last_name or ""
        pipeline.first_name = sp.first_name or ""
        pipeline.middle_name = sp.middle_name or ""
        pipeline.inn = sp.inn or ""
        pipeline.birth_date = sp.birth_date or ""

        # Регион
        if sp.region:
            pipeline.region_code = sp.region
            pipeline.region = REGIONS.get(sp.region, "")

        # Долги — из ФССП enforcements
        total = 0.0
        creditors_seen = {}
        for e in report.enforcements:
            claimant = e.claimant or "Неизвестный кредитор"
            amount = e.amount or 0
            total += amount
            if claimant in creditors_seen:
                creditors_seen[claimant]["amount"] += amount
            else:
                creditors_seen[claimant] = {
                    "name": claimant,
                    "amount": amount,
                    "debt_type": "tax" if (e.subject and "ФНС" in e.subject) else "credit",
                    "contract_number": e.number or "",
                    "source": "fssp",
                }

        pipeline.creditors = [CreditorInfo(**c) for c in creditors_seen.values()]
        pipeline.total_debt = total

        # Маршрут
        route = determine_route(total)
        pipeline.route = route.value
        if pipeline.region:
            pipeline.court_name = determine_court(pipeline.region)
        else:
            pipeline.court_name = "Арбитражный суд по месту жительства"

    _save_pipeline_data(pid, pipeline)
    return RedirectResponse(f"/pipeline/{pid}/assessment", status_code=302)


@app.get("/pipeline/{pid}/assessment", response_class=HTMLResponse)
async def pipeline_assessment_page(request: Request, pid: str):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    pipeline = _load_pipeline(pid)
    if not pipeline:
        return HTMLResponse("<h3>Pipeline не найден</h3>", status_code=404)

    # Рекомендации по ускорению
    accel_tips = recommend_acceleration(
        total_debt=pipeline.total_debt,
        monthly_income=pipeline.monthly_income,
        dependents_count=pipeline.dependents_count,
        is_pensioner=pipeline.is_pensioner,
        receives_benefits=pipeline.receives_benefits,
        ip_ended_art46=pipeline.ip_ended_art46,
        ip_longer_7_years=pipeline.ip_longer_7_years,
        route=pipeline.route,
    )

    ctx = _pipeline_context(pipeline, pid, 2)
    ctx["request"] = request
    ctx["user"] = user
    ctx["mfc_check"] = None
    ctx["accel_tips"] = accel_tips
    return templates.TemplateResponse("pipeline/assessment.html", ctx)


@app.post("/pipeline/{pid}/assessment", response_class=HTMLResponse)
async def pipeline_assessment_submit(request: Request, pid: str):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    pipeline = _load_pipeline(pid)
    if not pipeline:
        return HTMLResponse("<h3>Pipeline не найден</h3>", status_code=404)

    form = await request.form()
    action = form.get("action", "continue")

    # Обновляем данные МФЦ
    pipeline.ip_ended_art46 = bool(form.get("ip_ended_art46"))
    pipeline.ip_longer_7_years = bool(form.get("ip_longer_7_years"))
    pipeline.is_pensioner = bool(form.get("is_pensioner"))
    pipeline.receives_benefits = bool(form.get("receives_benefits"))
    pipeline.skip_restructuring = bool(form.get("skip_restructuring"))

    if action == "switch_court":
        pipeline.route = "court"
        if pipeline.region:
            pipeline.court_name = determine_court(pipeline.region)
        _save_pipeline_data(pid, pipeline)
        return RedirectResponse(f"/pipeline/{pid}/assessment", status_code=302)

    if action == "check_mfc":
        mfc_check = check_mfc_eligibility(
            pipeline.total_debt,
            pipeline.ip_ended_art46,
            pipeline.ip_longer_7_years,
            pipeline.is_pensioner,
            pipeline.receives_benefits,
        )
        _save_pipeline_data(pid, pipeline)
        ctx = _pipeline_context(pipeline, pid, 2)
        ctx["request"] = request
        ctx["user"] = user
        ctx["mfc_check"] = mfc_check
        return templates.TemplateResponse("pipeline/assessment.html", ctx)

    # continue — переход к profile
    pipeline.current_step = PipelineStep.PROFILE
    _save_pipeline_data(pid, pipeline)
    return RedirectResponse(f"/pipeline/{pid}/profile", status_code=302)


@app.get("/pipeline/{pid}/profile", response_class=HTMLResponse)
async def pipeline_profile_page(request: Request, pid: str):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    pipeline = _load_pipeline(pid)
    if not pipeline:
        return HTMLResponse("<h3>Pipeline не найден</h3>", status_code=404)

    ctx = _pipeline_context(pipeline, pid, 3)
    ctx["request"] = request
    ctx["user"] = user
    ctx["regions"] = sorted(ARBITRATION_COURTS.keys())
    return templates.TemplateResponse("pipeline/profile.html", ctx)


@app.post("/pipeline/{pid}/profile", response_class=HTMLResponse)
async def pipeline_profile_submit(request: Request, pid: str):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    pipeline = _load_pipeline(pid)
    if not pipeline:
        return HTMLResponse("<h3>Pipeline не найден</h3>", status_code=404)

    form = await request.form()

    # Личные данные
    pipeline.last_name = form.get("last_name", "")
    pipeline.first_name = form.get("first_name", "")
    pipeline.middle_name = form.get("middle_name", "")
    pipeline.birth_date = form.get("birth_date", "")
    pipeline.inn = form.get("inn", "")
    pipeline.snils = form.get("snils", "")

    # Паспорт
    pipeline.passport_series = form.get("passport_series", "")
    pipeline.passport_number = form.get("passport_number", "")
    pipeline.passport_issued_by = form.get("passport_issued_by", "")
    pipeline.passport_issued_date = form.get("passport_issued_date", "")
    pipeline.passport_code = form.get("passport_code", "")

    # Адрес
    pipeline.region = form.get("region", "")
    pipeline.city = form.get("city", "")
    pipeline.address = form.get("address", "")
    pipeline.phone = form.get("phone", "")
    pipeline.email = form.get("email", "")

    # Обновляем суд по новому региону
    if pipeline.region:
        pipeline.court_name = determine_court(pipeline.region)

    # Кредиторы
    creditors = []
    for i in range(50):
        name = form.get(f"cred_name_{i}", "")
        if not name:
            continue
        creditors.append(CreditorInfo(
            name=name,
            amount=float(form.get(f"cred_amount_{i}", 0) or 0),
            debt_type=form.get(f"cred_type_{i}", "credit"),
            contract_number=form.get(f"cred_contract_{i}", ""),
            creditor_inn=form.get(f"cred_inn_{i}", ""),
        ))
    pipeline.creditors = creditors

    # Пересчитываем total_debt
    pipeline.total_debt = sum(c.amount for c in creditors)

    # Имущество
    properties = []
    for i in range(30):
        ptype = form.get(f"prop_type_{i}", "")
        if not ptype:
            continue
        properties.append({
            "property_type": ptype,
            "description": form.get(f"prop_desc_{i}", ""),
            "estimated_value": float(form.get(f"prop_value_{i}", 0) or 0),
            "is_sole_housing": bool(form.get(f"prop_sole_{i}", "")),
        })
    pipeline.properties = properties

    # Доходы
    pipeline.employment_type = form.get("employment_type", "")
    pipeline.monthly_income = float(form.get("monthly_income", 0) or 0)
    pipeline.employer = form.get("employer", "")
    pipeline.dependents_count = int(form.get("dependents_count", 0) or 0)

    # Семья
    pipeline.marital_status = form.get("marital_status", "single")
    pipeline.children_count = int(form.get("children_count", 0) or 0)

    pipeline.current_step = PipelineStep.CREDITORS
    _save_pipeline_data(pid, pipeline)
    return RedirectResponse(f"/pipeline/{pid}/creditors", status_code=302)


# --- Шаг 4: Кредиторы ---


@app.get("/pipeline/{pid}/creditors", response_class=HTMLResponse)
async def pipeline_creditors_page(request: Request, pid: str):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    pipeline = _load_pipeline(pid)
    if not pipeline:
        return HTMLResponse("<h3>Pipeline не найден</h3>", status_code=404)

    ctx = _pipeline_context(pipeline, pid, 4)
    ctx["request"] = request
    ctx["user"] = user
    return templates.TemplateResponse("pipeline/creditors.html", ctx)


@app.post("/pipeline/{pid}/creditors", response_class=HTMLResponse)
async def pipeline_creditors_submit(request: Request, pid: str):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    pipeline = _load_pipeline(pid)
    if not pipeline:
        return HTMLResponse("<h3>Pipeline не найден</h3>", status_code=404)

    form = await request.form()

    creditors = []
    for i in range(50):
        name = form.get(f"cred_name_{i}", "")
        if not name:
            continue
        creditors.append(CreditorInfo(
            name=name,
            amount=float(form.get(f"cred_amount_{i}", 0) or 0),
            debt_type=form.get(f"cred_type_{i}", "credit"),
            contract_number=form.get(f"cred_contract_{i}", ""),
            creditor_inn=form.get(f"cred_inn_{i}", ""),
        ))
    pipeline.creditors = creditors
    pipeline.total_debt = sum(c.amount for c in creditors)

    # Обновляем маршрут по новой сумме
    route = determine_route(pipeline.total_debt)
    pipeline.route = route.value
    if pipeline.region:
        pipeline.court_name = determine_court(pipeline.region)

    pipeline.current_step = PipelineStep.DOCUMENTS
    _save_pipeline_data(pid, pipeline)
    return RedirectResponse(f"/pipeline/{pid}/documents", status_code=302)


@app.get("/pipeline/{pid}/documents", response_class=HTMLResponse)
async def pipeline_documents_page(request: Request, pid: str):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    pipeline = _load_pipeline(pid)
    if not pipeline:
        return HTMLResponse("<h3>Pipeline не найден</h3>", status_code=404)

    documents = get_documents_for_route(pipeline.route, pipeline.marital_status, pipeline.children_count)
    doc_statuses = {ds.doc_name: ds.is_ready for ds in pipeline.document_statuses}

    ctx = _pipeline_context(pipeline, pid, 5)
    ctx["request"] = request
    ctx["user"] = user
    ctx["documents"] = documents
    ctx["doc_statuses"] = doc_statuses
    return templates.TemplateResponse("pipeline/documents.html", ctx)


@app.post("/pipeline/{pid}/documents", response_class=HTMLResponse)
async def pipeline_documents_submit(request: Request, pid: str):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    pipeline = _load_pipeline(pid)
    if not pipeline:
        return HTMLResponse("<h3>Pipeline не найден</h3>", status_code=404)

    form = await request.form()

    # Собираем статусы документов
    statuses = []
    for i in range(30):
        doc_name = form.get(f"doc_{i}", "")
        if doc_name:
            statuses.append(DocumentStatus(doc_name=doc_name, is_ready=True))
    pipeline.document_statuses = statuses

    pipeline.current_step = PipelineStep.APPLICATION
    _save_pipeline_data(pid, pipeline)
    return RedirectResponse(f"/pipeline/{pid}/application", status_code=302)


@app.get("/pipeline/{pid}/application", response_class=HTMLResponse)
async def pipeline_application_page(request: Request, pid: str):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    pipeline = _load_pipeline(pid)
    if not pipeline:
        return HTMLResponse("<h3>Pipeline не найден</h3>", status_code=404)

    ctx = _pipeline_context(pipeline, pid, 6)
    ctx["request"] = request
    ctx["user"] = user
    ctx["show_petition"] = pipeline.skip_restructuring and pipeline.route == "court"
    return templates.TemplateResponse("pipeline/application.html", ctx)


@app.get("/pipeline/{pid}/download")
async def pipeline_download(request: Request, pid: str, doc: str = "application"):
    """Скачивание DOCX документов pipeline."""
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    pipeline = _load_pipeline(pid)
    if not pipeline:
        return HTMLResponse("<h3>Pipeline не найден</h3>", status_code=404)

    from debt_inspector.processing.application_generator import (
        generate_court_application,
        generate_mfc_application,
        generate_creditors_list,
        generate_inventory,
        generate_skip_restructuring_petition,
    )

    tmp_dir = Path(tempfile.mkdtemp())
    data = pipeline.model_dump(mode="json")
    name_part = pipeline.last_name or "debtor"

    if doc == "application":
        if pipeline.route == "mfc":
            path = generate_mfc_application(data, tmp_dir / f"zayavlenie_mfc_{name_part}.docx")
        else:
            path = generate_court_application(data, tmp_dir / f"zayavlenie_sud_{name_part}.docx")
    elif doc == "creditors":
        path = generate_creditors_list(data, tmp_dir / f"spisok_kreditorov_{name_part}.docx")
    elif doc == "inventory":
        path = generate_inventory(data, tmp_dir / f"opis_imuschestva_{name_part}.docx")
    elif doc == "petition":
        path = generate_skip_restructuring_petition(data, tmp_dir / f"hodatajstvo_{name_part}.docx")
    else:
        return HTMLResponse("<h3>Неизвестный тип документа</h3>", status_code=400)

    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=path.name,
    )


# --- Шаг 7: Оплата ---


@app.get("/pipeline/{pid}/payment", response_class=HTMLResponse)
async def pipeline_payment_page(request: Request, pid: str):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    pipeline = _load_pipeline(pid)
    if not pipeline:
        return HTMLResponse("<h3>Pipeline не найден</h3>", status_code=404)

    pipeline.current_step = PipelineStep.PAYMENT
    _save_pipeline_data(pid, pipeline)

    court_details = get_court_details(pipeline.court_name) if pipeline.court_name else None
    payer_name = pipeline.full_name_computed
    payer_inn = pipeline.inn

    fee_qr = generate_fee_qr(court_details, payer_name, payer_inn) if court_details else None
    deposit_qr = generate_deposit_qr(court_details, payer_name, payer_inn) if court_details else None

    ctx = _pipeline_context(pipeline, pid, 7)
    ctx["request"] = request
    ctx["user"] = user
    ctx["court_details"] = court_details
    ctx["fee_qr"] = fee_qr
    ctx["deposit_qr"] = deposit_qr
    ctx["is_mfc"] = pipeline.route == "mfc"
    return templates.TemplateResponse("pipeline/payment.html", ctx)


@app.get("/pipeline/{pid}/filing", response_class=HTMLResponse)
async def pipeline_filing_page(request: Request, pid: str):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    pipeline = _load_pipeline(pid)
    if not pipeline:
        return HTMLResponse("<h3>Pipeline не найден</h3>", status_code=404)

    pipeline.current_step = PipelineStep.FILING
    _save_pipeline_data(pid, pipeline)

    # СРО для региона
    sro_list = get_sro_for_region(pipeline.region) if pipeline.region else SRO_LIST
    # Реквизиты суда
    court_details = get_court_details(pipeline.court_name) if pipeline.court_name else None

    ctx = _pipeline_context(pipeline, pid, 8)
    ctx["request"] = request
    ctx["user"] = user
    ctx["sro_list"] = sro_list
    ctx["court_details"] = court_details
    ctx["is_mfc"] = pipeline.route == "mfc"
    return templates.TemplateResponse("pipeline/filing.html", ctx)


@app.post("/pipeline/{pid}/filing", response_class=HTMLResponse)
async def pipeline_filing_submit(request: Request, pid: str):
    """Сохранение выбранного СРО."""
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    pipeline = _load_pipeline(pid)
    if not pipeline:
        return HTMLResponse("<h3>Pipeline не найден</h3>", status_code=404)

    form = await request.form()
    pipeline.selected_sro = form.get("selected_sro", "")
    pipeline.selected_sro_address = form.get("selected_sro_address", "")
    pipeline.selected_sro_inn = form.get("selected_sro_inn", "")
    _save_pipeline_data(pid, pipeline)

    return RedirectResponse(f"/pipeline/{pid}/filing", status_code=302)


# --- AI-чат помощник ---


@app.post("/api/chat")
async def api_chat(request: Request):
    """AI-чат помощник по банкротству."""
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Не авторизован"}, status_code=401)

    body = await request.json()
    question = body.get("question", "").strip()
    step = body.get("step", "")
    context = body.get("context", "")

    if not question:
        return JSONResponse({"error": "Вопрос не может быть пустым"}, status_code=400)

    from debt_inspector.ai_chat import chat
    answer = await chat(question, step, context)
    return JSONResponse({"answer": answer})


# --- Калькулятор банкротства ---


@app.get("/calculator", response_class=HTMLResponse)
async def calculator_page(request: Request):
    user = _get_user(request)
    return templates.TemplateResponse("calculator.html", {"request": request, "user": user, "result": None})


@app.post("/calculator", response_class=HTMLResponse)
async def calculator_submit(
    request: Request,
    total_debt: float = Form(0),
    monthly_income: float = Form(0),
    dependents_count: int = Form(0),
    is_pensioner: str = Form(""),
    receives_benefits: str = Form(""),
    ip_ended_art46: str = Form(""),
    ip_longer_7_years: str = Form(""),
):
    user = _get_user(request)

    route = determine_route(total_debt)
    mfc_check = check_mfc_eligibility(
        total_debt,
        bool(ip_ended_art46),
        bool(ip_longer_7_years),
        bool(is_pensioner),
        bool(receives_benefits),
    )
    accel_tips = recommend_acceleration(
        total_debt=total_debt,
        monthly_income=monthly_income,
        dependents_count=dependents_count,
        is_pensioner=bool(is_pensioner),
        receives_benefits=bool(receives_benefits),
        ip_ended_art46=bool(ip_ended_art46),
        ip_longer_7_years=bool(ip_longer_7_years),
        route=route.value,
    )

    # Расходы
    if route.value == "mfc":
        expenses = 0
        timeline = "6 месяцев"
    else:
        expenses = 25_300
        subsistence = 17_733
        can_skip = monthly_income < subsistence * (1 + dependents_count)
        timeline = "3-6 месяцев" if can_skip else "6-12 месяцев"

    result = {
        "total_debt": total_debt,
        "monthly_income": monthly_income,
        "dependents_count": dependents_count,
        "route": route.value,
        "route_label": "Внесудебное (МФЦ)" if route.value == "mfc" else ("Судебное" if route.value == "court" else "Не рекомендуется"),
        "mfc_check": mfc_check,
        "expenses": expenses,
        "timeline": timeline,
        "accel_tips": accel_tips,
    }

    return templates.TemplateResponse("calculator.html", {"request": request, "user": user, "result": result})


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
