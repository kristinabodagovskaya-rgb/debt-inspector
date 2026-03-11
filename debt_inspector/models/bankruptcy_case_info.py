"""
Модель полной анкеты для процедуры банкротства.
"""

from pydantic import BaseModel, Field
from enum import Enum


class BankruptcyRoute(str, Enum):
    MFC = "mfc"                  # Внесудебное через МФЦ (до 500 000 руб.)
    COURT = "court"              # Судебное банкротство (от 500 000 руб.)
    UNKNOWN = "unknown"


class PropertyType(str, Enum):
    APARTMENT = "apartment"
    HOUSE = "house"
    CAR = "car"
    LAND = "land"
    OTHER = "other"


class PropertyItem(BaseModel):
    property_type: PropertyType = PropertyType.OTHER
    description: str = ""
    estimated_value: float = 0.0
    is_sole_housing: bool = False  # Единственное жильё (не подлежит реализации)


class IncomeInfo(BaseModel):
    employment_type: str = ""       # Работаю / не работаю / пенсионер / ИП
    monthly_income: float = 0.0
    employer: str = ""
    dependents_count: int = 0       # Кол-во иждивенцев


class DebtorProfile(BaseModel):
    """Полная анкета должника для банкротства."""

    # ФИО
    last_name: str = ""
    first_name: str = ""
    middle_name: str = ""
    birth_date: str = ""
    inn: str = ""
    snils: str = ""

    # Адрес (для подсудности)
    region: str = ""
    city: str = ""
    address: str = ""  # Полный адрес регистрации

    # Контакты
    phone: str = ""
    email: str = ""

    # Долги
    total_debt: float = 0.0
    debt_details: str = ""  # Описание долгов

    # Имущество
    properties: list[PropertyItem] = Field(default_factory=list)

    # Доходы
    income: IncomeInfo = Field(default_factory=IncomeInfo)

    # Результат анализа
    bankruptcy_route: BankruptcyRoute = BankruptcyRoute.UNKNOWN
    jurisdiction_court: str = ""  # Арбитражный суд по месту жительства

    @property
    def full_name(self) -> str:
        parts = [self.last_name, self.first_name, self.middle_name]
        return " ".join(p for p in parts if p)


# Арбитражные суды по регионам
ARBITRATION_COURTS = {
    "Москва": "Арбитражный суд города Москвы",
    "Московская область": "Арбитражный суд Московской области",
    "Санкт-Петербург": "Арбитражный суд города Санкт-Петербурга и Ленинградской области",
    "Ленинградская область": "Арбитражный суд города Санкт-Петербурга и Ленинградской области",
    "Краснодарский край": "Арбитражный суд Краснодарского края",
    "Нижегородская область": "Арбитражный суд Нижегородской области",
    "Республика Татарстан": "Арбитражный суд Республики Татарстан",
    "Свердловская область": "Арбитражный суд Свердловской области",
    "Самарская область": "Арбитражный суд Самарской области",
    "Ростовская область": "Арбитражный суд Ростовской области",
    "Челябинская область": "Арбитражный суд Челябинской области",
    "Республика Башкортостан": "Арбитражный суд Республики Башкортостан",
    "Пермский край": "Арбитражный суд Пермского края",
    "Волгоградская область": "Арбитражный суд Волгоградской области",
    "Новосибирская область": "Арбитражный суд Новосибирской области",
    "Красноярский край": "Арбитражный суд Красноярского края",
    "Кемеровская область": "Арбитражный суд Кемеровской области",
    "Оренбургская область": "Арбитражный суд Оренбургской области",
    "Ставропольский край": "Арбитражный суд Ставропольского края",
    "Воронежская область": "Арбитражный суд Воронежской области",
    "Тюменская область": "Арбитражный суд Тюменской области",
    "Омская область": "Арбитражный суд Омской области",
    "Саратовская область": "Арбитражный суд Саратовской области",
    "Иркутская область": "Арбитражный суд Иркутской области",
    "Хабаровский край": "Арбитражный суд Хабаровского края",
    "Приморский край": "Арбитражный суд Приморского края",
    "Калининградская область": "Арбитражный суд Калининградской области",
    "Тульская область": "Арбитражный суд Тульской области",
    "Рязанская область": "Арбитражный суд Рязанской области",
    "Ярославская область": "Арбитражный суд Ярославской области",
}


def determine_route(total_debt: float) -> BankruptcyRoute:
    """Определяет маршрут банкротства по сумме долга."""
    if total_debt <= 0:
        return BankruptcyRoute.UNKNOWN
    if 25_000 <= total_debt <= 500_000:
        return BankruptcyRoute.MFC
    return BankruptcyRoute.COURT


def determine_court(region: str) -> str:
    """Определяет арбитражный суд по региону."""
    # Прямое совпадение
    if region in ARBITRATION_COURTS:
        return ARBITRATION_COURTS[region]

    # Частичное совпадение
    region_lower = region.lower()
    for key, court in ARBITRATION_COURTS.items():
        if key.lower() in region_lower or region_lower in key.lower():
            return court

    return f"Арбитражный суд ({region}) — уточните на сайте arbitr.ru"
