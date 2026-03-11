from pydantic import BaseModel, Field
from enum import Enum


class SubjectType(str, Enum):
    PERSON = "person"
    COMPANY = "company"


class SearchParams(BaseModel):
    """Параметры поиска должника."""

    subject_type: SubjectType = SubjectType.PERSON

    # Физлицо
    last_name: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    birth_date: str | None = Field(None, description="Дата рождения ДД.ММ.ГГГГ")

    # Юрлицо
    company_name: str | None = None

    # Общие
    inn: str | None = None
    ogrn: str | None = None
    region: int | None = Field(None, description="Код региона ФССП (1-99)")

    @property
    def full_name(self) -> str:
        parts = [self.last_name, self.first_name, self.middle_name]
        return " ".join(p for p in parts if p)

    @property
    def display_name(self) -> str:
        if self.subject_type == SubjectType.COMPANY:
            return self.company_name or self.inn or "—"
        return self.full_name or self.inn or "—"


class DebtorInfo(BaseModel):
    """Сводная информация о должнике."""

    search_params: SearchParams
    total_debt: float = 0.0
    active_enforcements: int = 0
    court_cases_count: int = 0
    is_bankrupt: bool = False
