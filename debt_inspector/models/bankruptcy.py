from pydantic import BaseModel
from datetime import date
from enum import Enum


class BankruptcyPhase(str, Enum):
    OBSERVATION = "observation"           # Наблюдение
    RESTRUCTURING = "restructuring"       # Реструктуризация
    SALE = "sale"                         # Реализация имущества
    SETTLEMENT = "settlement"             # Мировое соглашение
    COMPLETED = "completed"               # Завершено
    UNKNOWN = "unknown"


class BankruptcyCase(BaseModel):
    """Дело о банкротстве (ЕФРСБ)."""

    debtor_name: str | None = None
    debtor_inn: str | None = None
    debtor_address: str | None = None
    case_number: str | None = None
    phase: BankruptcyPhase = BankruptcyPhase.UNKNOWN
    arbitration_manager: str | None = None
    arbitration_manager_inn: str | None = None
    court_name: str | None = None
    date_decision: date | None = None
    date_completion: date | None = None
    messages_count: int | None = None
    source_url: str | None = None
