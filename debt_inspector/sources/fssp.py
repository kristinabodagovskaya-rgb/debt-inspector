"""
Парсер ФССП (Федеральная служба судебных приставов).

Использует публичный поиск на fssp.gov.ru.
ФССП отдаёт результаты через POST + AJAX с задержкой.
Капча решается автоматически через rucaptcha/anti-captcha (если настроен CAPTCHA_API_KEY).
"""

import asyncio
import re
from datetime import date
from bs4 import BeautifulSoup

from .base import BaseSource
from debt_inspector.models.debtor import SearchParams, SubjectType
from debt_inspector.models.enforcement import EnforcementProceeding, EnforcementStatus
from debt_inspector.captcha import (
    CaptchaSolver,
    extract_captcha_image_url,
    extract_recaptcha_sitekey,
)


# Коды регионов ФССП (основные)
FSSP_REGIONS = {
    77: "Москва", 78: "Санкт-Петербург", 50: "Московская область",
    47: "Ленинградская область", 23: "Краснодарский край",
    52: "Нижегородская область", 16: "Республика Татарстан",
    66: "Свердловская область", 63: "Самарская область",
    61: "Ростовская область",
}

MAX_CAPTCHA_RETRIES = 3


class FSSPSource(BaseSource):
    name = "fssp"
    base_url = "https://fssp.gov.ru"

    def __init__(self):
        super().__init__()
        self.captcha_solver = CaptchaSolver()

    async def close(self):
        await self.captcha_solver.close()
        await super().close()

    async def search(self, params: SearchParams) -> list[EnforcementProceeding]:
        """Поиск исполнительных производств."""
        results = []

        try:
            if params.subject_type == SubjectType.PERSON:
                results = await self._search_person(params)
            else:
                results = await self._search_company(params)
        except Exception as e:
            raise RuntimeError(f"ФССП: ошибка поиска — {e}") from e

        return results

    async def _search_person(self, params: SearchParams) -> list[EnforcementProceeding]:
        """Поиск по физлицу."""
        if not params.last_name:
            return []

        form_data = {
            "is": "1",
            "region_id": str(params.region or 0),
            "name": params.last_name or "",
            "firstname": params.first_name or "",
            "secondname": params.middle_name or "",
            "date": params.birth_date or "",
        }

        return await self._do_search(form_data)

    async def _search_company(self, params: SearchParams) -> list[EnforcementProceeding]:
        """Поиск по юрлицу."""
        name = params.company_name or ""
        if not name and not params.inn:
            return []

        form_data = {
            "is": "1",
            "region_id": str(params.region or 0),
            "name_company": name,
        }

        return await self._do_search(form_data)

    async def _do_search(self, form_data: dict) -> list[EnforcementProceeding]:
        """Отправить запрос поиска, решить капчу если нужно, распарсить результаты."""
        url = f"{self.base_url}/iss/ip"

        # Первый запрос — получить страницу с формой (и cookies)
        await self._get(self.base_url)
        await asyncio.sleep(1)

        # POST запрос поиска
        resp = await self._post(url, data=form_data)

        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")

        html = resp.text

        # Проверка на капчу и попытка решить
        if self._has_captcha(html):
            html = await self._solve_and_retry(html, url, form_data)

        return self._parse_results(html)

    def _has_captcha(self, html: str) -> bool:
        """Проверяет наличие капчи на странице."""
        lower = html.lower()
        return "captcha" in lower or "recaptcha" in lower or "g-recaptcha" in lower

    async def _solve_and_retry(
        self, html: str, url: str, form_data: dict
    ) -> str:
        """Решает капчу и повторяет запрос. Возвращает HTML с результатами."""
        if not self.captcha_solver.is_configured:
            raise RuntimeError(
                "ФССП требует капчу. Установите CAPTCHA_API_KEY "
                "(rucaptcha.com или anti-captcha.com) и CAPTCHA_PROVIDER"
            )

        for attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
            # reCAPTCHA v2
            sitekey = extract_recaptcha_sitekey(html)
            if sitekey:
                token = await self.captcha_solver.solve_recaptcha_v2(sitekey, url)
                if not token:
                    raise RuntimeError(f"Не удалось решить reCAPTCHA (попытка {attempt})")

                form_data["g-recaptcha-response"] = token
                resp = await self._post(url, data=form_data)
                html = resp.text

                if not self._has_captcha(html):
                    return html
                continue

            # Обычная картинка-капча
            img_url = extract_captcha_image_url(html)
            if img_url:
                if img_url.startswith("/"):
                    img_url = f"{self.base_url}{img_url}"

                img_resp = await self._get(img_url)
                captcha_text = await self.captcha_solver.solve_image(img_resp.content)
                if not captcha_text:
                    raise RuntimeError(f"Не удалось решить капчу-картинку (попытка {attempt})")

                form_data["captcha"] = captcha_text
                resp = await self._post(url, data=form_data)
                html = resp.text

                if not self._has_captcha(html):
                    return html
                continue

            # Капча есть, но не можем определить тип
            raise RuntimeError("Неизвестный тип капчи ФССП")

        raise RuntimeError(f"Не удалось решить капчу за {MAX_CAPTCHA_RETRIES} попыток")

    def _parse_results(self, html: str) -> list[EnforcementProceeding]:
        """Парсинг таблицы результатов ФССП."""
        soup = BeautifulSoup(html, "lxml")
        results = []

        # ФССП отображает результаты в таблице .results
        table = soup.select_one("table.results") or soup.select_one(".iss-result table")
        if not table:
            # Пробуем альтернативную структуру — блоки .results-frame
            return self._parse_results_blocks(soup)

        rows = table.select("tr")[1:]  # skip header
        for row in rows:
            cells = row.select("td")
            if len(cells) < 6:
                continue

            proc = self._parse_row(cells)
            if proc:
                results.append(proc)

        return results

    def _parse_results_blocks(self, soup: BeautifulSoup) -> list[EnforcementProceeding]:
        """Альтернативный парсинг — блочная вёрстка."""
        results = []
        blocks = soup.select(".iss-result .row") or soup.select("[class*='result']")

        for block in blocks:
            text = block.get_text(separator="\n", strip=True)
            if not text or len(text) < 20:
                continue

            proc = EnforcementProceeding()

            # Номер ИП
            num_match = re.search(r"(\d+/\d+/\d+-\w+)", text)
            if num_match:
                proc.number = num_match.group(1)

            # Сумма
            amount_match = re.search(r"(\d[\d\s]*[.,]\d{2})\s*руб", text)
            if amount_match:
                amount_str = amount_match.group(1).replace(" ", "").replace(",", ".")
                try:
                    proc.amount = float(amount_str)
                except ValueError:
                    pass

            # Дата
            date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
            if date_match:
                try:
                    proc.date_opened = date(
                        *map(int, reversed(date_match.group(1).split(".")))
                    )
                except (ValueError, TypeError):
                    pass

            # Предмет
            subj_match = re.search(
                r"(?:Предмет|Исполнение)[:\s]*(.+?)(?:\n|$)", text
            )
            if subj_match:
                proc.subject = subj_match.group(1).strip()[:200]

            # Статус
            if "окончено" in text.lower() or "исполнено" in text.lower():
                proc.status = EnforcementStatus.FINISHED
            elif "приостановлено" in text.lower():
                proc.status = EnforcementStatus.SUSPENDED
            elif proc.number:
                proc.status = EnforcementStatus.ACTIVE

            if proc.number:
                results.append(proc)

        return results

    def _parse_row(self, cells: list) -> EnforcementProceeding | None:
        """Парсинг строки таблицы."""
        try:
            text_cells = [c.get_text(strip=True) for c in cells]

            proc = EnforcementProceeding()

            # Колонки: №, Должник, ИП, Предмет, Отдел, Пристав
            if len(text_cells) >= 3:
                proc.number = text_cells[2] if len(text_cells) > 2 else None
                proc.subject = text_cells[3] if len(text_cells) > 3 else None
                proc.department = text_cells[4] if len(text_cells) > 4 else None
                proc.bailiff = text_cells[5] if len(text_cells) > 5 else None

            # Извлечение суммы из предмета
            if proc.subject:
                amount_match = re.search(
                    r"(\d[\d\s]*[.,]\d{2})", proc.subject
                )
                if amount_match:
                    amount_str = amount_match.group(1).replace(" ", "").replace(",", ".")
                    try:
                        proc.amount = float(amount_str)
                    except ValueError:
                        pass

            # Дата из номера ИП (формат: NNNN/YY/NNNNN-ИП)
            if proc.number:
                date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", str(cells[2]))
                if date_match:
                    try:
                        proc.date_opened = date(
                            *map(int, reversed(date_match.group(1).split(".")))
                        )
                    except (ValueError, TypeError):
                        pass

                proc.status = EnforcementStatus.ACTIVE

            return proc if proc.number else None

        except (IndexError, AttributeError):
            return None
