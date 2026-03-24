"""
Модели pipeline банкротства — от поиска долгов до подачи в суд.
"""

from enum import Enum
from pydantic import BaseModel, Field


class PipelineStep(str, Enum):
    ASSESSMENT = "assessment"      # Шаг 2: Оценка ситуации + маршрут
    PROFILE = "profile"            # Шаг 3: Анкета должника
    CREDITORS = "creditors"        # Шаг 4: Список кредиторов
    DOCUMENTS = "documents"        # Шаг 5: Чек-лист документов
    APPLICATION = "application"    # Шаг 6: Генерация заявления
    PAYMENT = "payment"            # Шаг 7: Оплата госпошлины + депозит
    FILING = "filing"              # Шаг 8: Подача + инструкции


PIPELINE_STEPS = [
    {"key": "search", "label": "Поиск долгов", "num": 1},
    {"key": "assessment", "label": "Оценка", "num": 2},
    {"key": "profile", "label": "Анкета", "num": 3},
    {"key": "creditors", "label": "Кредиторы", "num": 4},
    {"key": "documents", "label": "Документы", "num": 5},
    {"key": "application", "label": "Заявление", "num": 6},
    {"key": "payment", "label": "Оплата", "num": 7},
    {"key": "filing", "label": "Подача", "num": 8},
]


class CreditorInfo(BaseModel):
    """Информация о кредиторе."""
    name: str = ""
    amount: float = 0.0
    debt_type: str = "credit"          # credit, microloan, mortgage, tax, utility, alimony, fine, other
    contract_number: str = ""          # Номер договора / ИП
    creditor_inn: str = ""             # ИНН кредитора
    status: str = "active"             # active, enforcement, court, overdue
    source: str = ""                   # fssp, manual


class RequiredDocument(BaseModel):
    """Требуемый документ для банкротства."""
    name: str
    description: str = ""
    where_to_get: str = ""
    is_critical: bool = True           # Без него нельзя подать
    route: str = "both"                # court, mfc, both


class DocumentStatus(BaseModel):
    """Статус готовности документа."""
    doc_name: str
    is_ready: bool = False


class BankruptcyPipeline(BaseModel):
    """Состояние pipeline банкротства."""
    report_id: str = ""
    current_step: PipelineStep = PipelineStep.ASSESSMENT

    # Данные из поиска
    full_name: str = ""
    inn: str = ""
    region: str = ""
    region_code: int = 0
    total_debt: float = 0.0

    # Маршрут
    route: str = ""                    # mfc / court
    court_name: str = ""

    # Кредиторы (из ФССП + ручные)
    creditors: list[CreditorInfo] = Field(default_factory=list)

    # Профиль должника
    last_name: str = ""
    first_name: str = ""
    middle_name: str = ""
    birth_date: str = ""
    snils: str = ""

    # Паспорт
    passport_series: str = ""
    passport_number: str = ""
    passport_issued_by: str = ""
    passport_issued_date: str = ""
    passport_code: str = ""            # Код подразделения

    # Адрес
    city: str = ""
    address: str = ""
    phone: str = ""
    email: str = ""

    # Имущество
    properties: list[dict] = Field(default_factory=list)

    # Доходы
    employment_type: str = ""
    monthly_income: float = 0.0
    employer: str = ""
    dependents_count: int = 0

    # Семья
    marital_status: str = "single"
    children_count: int = 0

    # МФЦ eligibility
    ip_ended_art46: bool = False       # ИП окончено по ст. 46
    ip_longer_7_years: bool = False    # ИП длится >7 лет
    is_pensioner: bool = False         # Пенсионер
    receives_benefits: bool = False    # Получает пособие

    # Документы
    document_statuses: list[DocumentStatus] = Field(default_factory=list)

    # Ускорение процедуры
    skip_restructuring: bool = False   # Ходатайство о пропуске реструктуризации (ст. 213.6 п.8)
    acceleration_tips: list[str] = Field(default_factory=list)

    # Выбранное СРО арбитражных управляющих
    selected_sro: str = ""
    selected_sro_address: str = ""
    selected_sro_inn: str = ""

    @property
    def full_name_computed(self) -> str:
        parts = [self.last_name, self.first_name, self.middle_name]
        return " ".join(p for p in parts if p)


# Список документов для судебного банкротства
COURT_DOCUMENTS = [
    RequiredDocument(
        name="Паспорт гражданина РФ",
        description="Копия всех страниц",
        where_to_get="Копия вашего паспорта (все заполненные страницы)",
        is_critical=True,
        route="both",
    ),
    RequiredDocument(
        name="ИНН (свидетельство)",
        description="Свидетельство о постановке на учёт в ФНС",
        where_to_get="Копия свидетельства или скачать с nalog.ru",
        is_critical=True,
        route="both",
    ),
    RequiredDocument(
        name="СНИЛС",
        description="Страховое свидетельство",
        where_to_get="Копия СНИЛС или выписка из ПФР через Госуслуги",
        is_critical=True,
        route="both",
    ),
    RequiredDocument(
        name="Справка 2-НДФЛ за 3 года",
        description="Справки о доходах за последние 3 года",
        where_to_get="У работодателя или через Личный кабинет ФНС (nalog.ru)",
        is_critical=True,
        route="court",
    ),
    RequiredDocument(
        name="Выписка из ЕГРН",
        description="Сведения о зарегистрированной недвижимости",
        where_to_get="Росреестр (rosreestr.gov.ru) или МФЦ, госпошлина 350 руб.",
        is_critical=True,
        route="court",
    ),
    RequiredDocument(
        name="Справка ГИБДД",
        description="О наличии/отсутствии зарегистрированных ТС",
        where_to_get="ГИБДД или через Госуслуги",
        is_critical=True,
        route="court",
    ),
    RequiredDocument(
        name="Выписки из банков",
        description="По всем счетам за последние 3 года",
        where_to_get="В каждом банке, где есть счета (через отделение или онлайн-банк)",
        is_critical=True,
        route="court",
    ),
    RequiredDocument(
        name="Копии кредитных договоров",
        description="Все договоры с кредиторами",
        where_to_get="Ваши экземпляры договоров или запросить копии у кредиторов",
        is_critical=True,
        route="court",
    ),
    RequiredDocument(
        name="Список кредиторов и должников",
        description="По форме Приложения к Приказу Минэкономразвития N 530",
        where_to_get="Будет сформирован автоматически на шаге 5",
        is_critical=True,
        route="court",
    ),
    RequiredDocument(
        name="Опись имущества",
        description="По форме Приложения к Приказу Минэкономразвития N 530",
        where_to_get="Будет сформирована автоматически на шаге 5",
        is_critical=True,
        route="court",
    ),
    RequiredDocument(
        name="Справка из ПФР (СФР)",
        description="Выписка из индивидуального лицевого счёта",
        where_to_get="Через Госуслуги или отделение СФР",
        is_critical=True,
        route="court",
    ),
    RequiredDocument(
        name="Справка о статусе ИП",
        description="Что не являетесь ИП (или выписка из ЕГРИП)",
        where_to_get="ФНС (nalog.ru) — сервис 'Предоставление сведений из ЕГРИП'",
        is_critical=True,
        route="court",
    ),
    RequiredDocument(
        name="Госпошлина 300 руб.",
        description="Квитанция об оплате госпошлины",
        where_to_get="Оплатить на сайте суда или в банке. Реквизиты — на сайте арбитражного суда",
        is_critical=True,
        route="court",
    ),
    RequiredDocument(
        name="Депозит 25 000 руб.",
        description="Внесение на депозит арбитражного суда (на вознаграждение управляющего)",
        where_to_get="Перевести на депозитный счёт суда. Реквизиты — на сайте арбитражного суда",
        is_critical=True,
        route="court",
    ),
    RequiredDocument(
        name="Свидетельство о браке / разводе",
        description="Если состоите или состояли в браке",
        where_to_get="Копия свидетельства из ЗАГСа",
        is_critical=False,
        route="court",
    ),
    RequiredDocument(
        name="Брачный договор",
        description="При наличии",
        where_to_get="Ваш экземпляр",
        is_critical=False,
        route="court",
    ),
    RequiredDocument(
        name="Свидетельства о рождении детей",
        description="Для несовершеннолетних детей",
        where_to_get="Копии свидетельств",
        is_critical=False,
        route="court",
    ),
]

# Документы для МФЦ
MFC_DOCUMENTS = [
    RequiredDocument(
        name="Паспорт гражданина РФ",
        description="Оригинал + копия",
        where_to_get="Оригинал паспорта (возьмите с собой)",
        is_critical=True,
        route="mfc",
    ),
    RequiredDocument(
        name="ИНН (свидетельство)",
        description="Копия или номер ИНН",
        where_to_get="Копия свидетельства или знание номера ИНН",
        is_critical=True,
        route="mfc",
    ),
    RequiredDocument(
        name="СНИЛС",
        description="Копия или номер СНИЛС",
        where_to_get="Копия СНИЛС",
        is_critical=True,
        route="mfc",
    ),
    RequiredDocument(
        name="Список кредиторов",
        description="С указанием сумм и реквизитов",
        where_to_get="Будет сформирован автоматически на шаге 5",
        is_critical=True,
        route="mfc",
    ),
    RequiredDocument(
        name="Справка ФССП об окончании ИП",
        description="Подтверждение окончания исполнительных производств по ст. 46",
        where_to_get="Запросить у судебного пристава или через Госуслуги",
        is_critical=False,
        route="mfc",
    ),
]


def get_documents_for_route(route: str, marital_status: str = "single", children_count: int = 0) -> list[RequiredDocument]:
    """Возвращает список документов в зависимости от маршрута и семейного статуса."""
    if route == "mfc":
        return MFC_DOCUMENTS[:]

    docs = [d for d in COURT_DOCUMENTS if d.route in ("court", "both")]

    # Убираем документы о браке если не женат
    if marital_status == "single":
        docs = [d for d in docs if d.name not in ("Свидетельство о браке / разводе", "Брачный договор")]

    # Убираем свидетельства детей если нет детей
    if children_count == 0:
        docs = [d for d in docs if d.name != "Свидетельства о рождении детей"]

    return docs
