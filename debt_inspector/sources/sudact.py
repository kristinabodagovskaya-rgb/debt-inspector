"""
Парсер судебных актов общей юрисдикции через sudact.ru.

Использует AJAX endpoint /regular/doc_ajax/ с polling.
Для мировых судей: /magistrate/doc_ajax/
"""

import asyncio
import re
from datetime import date
from bs4 import BeautifulSoup

from .base import BaseSource
from debt_inspector.models.debtor import SearchParams, SubjectType
from debt_inspector.models.court_case import CourtCase, CourtType, CourtCaseStatus

MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


# Маппинг кодов регионов ФССП → sudact.ru
REGION_TO_SUDACT = {
    77: "1011",  # Москва
    78: "1013",  # Санкт-Петербург
    50: "1012",  # Московская область
    47: "1014",  # Ленинградская область
    23: "1001",  # Краснодарский край
    52: "1007",  # Нижегородская область
    16: "1064",  # Республика Татарстан
    66: "1016",  # Свердловская область
    63: "1000",  # Самарская область
    61: "1008",  # Ростовская область
    74: "1077",  # Челябинская область
    2: "1053",   # Республика Башкортостан
    59: "1048",  # Пермский край
    34: "1002",  # Волгоградская область
    54: "1015",  # Новосибирская область
    24: "1037",  # Красноярский край
    42: "1010",  # Кемеровская область
    56: "1045",  # Оренбургская область
    26: "1033",  # Ставропольский край
    36: "1026",  # Воронежская область
}


class SudactSource(BaseSource):
    name = "sudact"
    base_url = "https://sudact.ru"

    async def search(self, params: SearchParams) -> list[CourtCase]:
        """Поиск судебных актов по ФИО или названию компании."""
        query = self._build_query(params)
        if not query:
            return []

        region_code = REGION_TO_SUDACT.get(params.region) if params.region else None

        results = []

        # Суды общей юрисдикции
        try:
            cases = await self._search_section("regular", query, region_code)
            results.extend(cases)
        except Exception:
            pass

        # Мировые судьи
        try:
            cases = await self._search_section("magistrate", query, region_code)
            for c in cases:
                c.court_type = CourtType.MAGISTRATE
            results.extend(cases)
        except Exception:
            pass

        return results

    def _build_query(self, params: SearchParams) -> str:
        """Формирует поисковый запрос.

        sudact.ru ищет в полном тексте решений. По ФИО часто не находит,
        поэтому ищем только по фамилии.
        """
        if params.subject_type == SubjectType.COMPANY:
            return params.company_name or params.inn or ""

        return params.last_name or ""

    async def _search_section(self, section: str, query: str, region_code: str | None = None) -> list[CourtCase]:
        """Поиск в разделе (regular/magistrate) с polling."""
        # Инициализация сессии
        await self._get(f"{self.base_url}/{section}/doc/")
        await asyncio.sleep(0.3)

        # Первый запрос — запускает поиск
        ajax_url = f"{self.base_url}/{section}/doc_ajax/"
        params = {f"{section}-txt": query}
        if region_code:
            params[f"{section}-area"] = region_code

        resp = await self._get(ajax_url, params=params, headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/{section}/doc/",
        })

        data = resp.json()

        # Polling — ждём результатов (до 5 попыток)
        timeout = 0.5
        for _ in range(5):
            if data.get("content"):
                break
            if data.get("status") == "new" or data.get("search_status") == "new":
                await asyncio.sleep(timeout)
                timeout *= 1.5
                resp = await self._get(ajax_url, params=params, headers={
                    "X-Requested-With": "XMLHttpRequest",
                })
                data = resp.json()
            else:
                break

        content = data.get("content", "")
        if not content:
            return []

        return self._parse_results(content, section)

    def _parse_results(self, html: str, section: str) -> list[CourtCase]:
        """Парсит HTML со списком судебных актов."""
        soup = BeautifulSoup(html, "lxml")
        results = []

        for li in soup.select("li"):
            a = li.select_one("a")
            if not a:
                continue

            link_text = a.get_text(strip=True)
            if len(link_text) < 10:
                continue

            href = a.get("href", "")
            full_text = li.get_text(separator=" ", strip=True)

            case = CourtCase(
                court_type=CourtType.GENERAL,
            )

            # Номер дела
            case_match = re.search(r"по делу\s*№?\s*(\S+)", full_text)
            if case_match:
                case.case_number = case_match.group(1).rstrip(",;.")

            # Номер решения (fallback)
            if not case.case_number:
                num_match = re.search(r"№\s*(\S+)", link_text)
                if num_match:
                    case.case_number = num_match.group(1).rstrip(",;.")

            # Дата из текста ссылки: "от 11 февраля 2019 г."
            date_match = re.search(
                r"от\s+(\d{1,2})\s+(\w+)\s+(\d{4})", link_text
            )
            if date_match:
                day = int(date_match.group(1))
                month_name = date_match.group(2).lower()
                year = int(date_match.group(3))
                month = MONTHS_RU.get(month_name)
                if month:
                    try:
                        case.date_filed = date(year, month, day)
                    except ValueError:
                        pass

            # Суд — текст после ссылки
            court_text = full_text.split(link_text)[-1].strip() if link_text in full_text else ""
            # Убираем номер в начале
            court_text = re.sub(r"^\d+\.\s*", "", court_text).strip()
            if court_text:
                # Парсим суд и категорию
                parts = court_text.split(" - ", 1)
                case.court_name = parts[0].strip()[:80]
                if len(parts) > 1:
                    case.subject = parts[1].strip()[:200]

            # URL
            if href:
                case.source_url = f"{self.base_url}{href}" if href.startswith("/") else href

            # Тип документа → статус
            lower = link_text.lower()
            if "решение" in lower or "определение" in lower or "постановление" in lower:
                case.status = CourtCaseStatus.DECIDED
            elif "апелляц" in lower:
                case.status = CourtCaseStatus.APPEALED
            else:
                case.status = CourtCaseStatus.DECIDED  # sudact.ru — это банк решений

            if case.case_number or case.court_name:
                results.append(case)

        return results
