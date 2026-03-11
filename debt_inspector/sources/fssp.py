"""
Парсер ФССП (Федеральная служба судебных приставов).

Рабочий endpoint: https://is-go.fssp.gov.ru/ajax_search (GET, JSONP).
Капча обязательна — решается через rucaptcha/anti-captcha.
"""

import asyncio
import json
import re
from datetime import date
from bs4 import BeautifulSoup

from .base import BaseSource
from debt_inspector.models.debtor import SearchParams, SubjectType
from debt_inspector.models.enforcement import EnforcementProceeding, EnforcementStatus
from debt_inspector.captcha import CaptchaSolver

SEARCH_URL = "https://is-go.fssp.gov.ru/ajax_search"
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
        try:
            # Сначала загружаем страницу для cookies
            await self._get(f"{self.base_url}/iss/ip")
            await asyncio.sleep(0.5)

            query_params = self._build_params(params)
            if not query_params:
                return []

            return await self._do_search(query_params)
        except Exception as e:
            raise RuntimeError(f"ФССП: {e}") from e

    def _build_params(self, params: SearchParams) -> dict | None:
        """Формирует параметры запроса."""
        base = {
            "system": "ip",
            "nocache": "1",
            "is[extended]": "1",
        }

        if params.region:
            base["is[region_id][0]"] = str(params.region)

        # Поиск по ИНН (вариант 5)
        if params.inn:
            return {**base, "is[variant]": "5", "is[inn]": params.inn}

        # Физлицо (вариант 1)
        if params.subject_type == SubjectType.PERSON:
            if not params.last_name:
                return None
            result = {
                **base,
                "is[variant]": "1",
                "is[last_name]": params.last_name or "",
                "is[first_name]": params.first_name or "",
                "is[patronymic]": params.middle_name or "",
            }
            if params.birth_date:
                result["is[date]"] = params.birth_date
            return result

        # Юрлицо (вариант 2)
        if params.company_name:
            return {
                **base,
                "is[variant]": "2",
                "is[drtr_name]": params.company_name,
            }

        return None

    async def _do_search(self, query_params: dict) -> list[EnforcementProceeding]:
        """Выполняет запрос и обрабатывает капчу."""
        callback_name = "jsonp_cb"
        query_params["callback"] = callback_name

        resp = await self._get(SEARCH_URL, params=query_params)
        data = self._parse_jsonp(resp.text, callback_name)

        if not data:
            return []

        html = data.get("data", "")

        # Проверяем нет ли ошибки "заполните поля"
        if not html or "Заполните" in html or len(html) < 50:
            return []

        # Капча?
        if "captcha-popup" in html:
            html = await self._handle_captcha(html, query_params, callback_name)

        return self._parse_results(html)

    async def _handle_captcha(self, html: str, query_params: dict, callback_name: str) -> str:
        """Решает капчу и повторяет запрос."""
        if not self.captcha_solver.is_configured:
            raise RuntimeError(
                "ФССП требует капчу. Установите CAPTCHA_API_KEY и CAPTCHA_PROVIDER"
            )

        for attempt in range(MAX_CAPTCHA_RETRIES):
            # Извлекаем base64 картинку
            img_match = re.search(r'src="data:image/png;base64,([^"]+)"', html)
            if not img_match:
                raise RuntimeError("Не удалось найти картинку капчи")

            import base64
            img_bytes = base64.b64decode(img_match.group(1))

            # Извлекаем code_id
            code_id_match = re.search(r'code_id=([^&"]+)', html)
            code_id = code_id_match.group(1) if code_id_match else ""

            # Решаем капчу
            captcha_text = await self.captcha_solver.solve_image(img_bytes)
            if not captcha_text:
                continue

            # Повторяем запрос с кодом капчи
            captcha_params = {**query_params}
            captcha_params["code_id"] = code_id
            captcha_params["code"] = captcha_text
            captcha_params["callback"] = callback_name

            resp = await self._get(SEARCH_URL, params=captcha_params)
            data = self._parse_jsonp(resp.text, callback_name)

            if not data:
                continue

            new_html = data.get("data", "")
            if "captcha-popup" not in new_html:
                return new_html

            html = new_html  # Попробуем снова с новой капчей

        raise RuntimeError(f"Не удалось решить капчу за {MAX_CAPTCHA_RETRIES} попыток")

    def _parse_jsonp(self, text: str, callback_name: str) -> dict | None:
        """Парсит JSONP ответ."""
        # Формат: callback_name({...})
        text = text.strip()

        # Убираем callback обёртку
        prefix = f"{callback_name}("
        if text.startswith(prefix) and text.endswith(")"):
            json_str = text[len(prefix):-1]
        else:
            # Пробуем найти JSON в тексте
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                json_str = match.group(0)
            else:
                return None

        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return None

    def _parse_results(self, html: str) -> list[EnforcementProceeding]:
        """Парсинг HTML результатов."""
        soup = BeautifulSoup(html, "lxml")
        results = []

        # ФССП возвращает результаты в таблице или блоках .iss-result
        table = soup.select_one("table") or soup.select_one(".results")
        if table:
            rows = table.select("tr")
            for row in rows:
                cells = row.select("td")
                if len(cells) >= 4:
                    proc = self._parse_row(cells)
                    if proc:
                        results.append(proc)

        # Альтернативно: блоки с классами
        if not results:
            blocks = soup.select(".iss-result") or soup.select("[class*='result']")
            for block in blocks:
                proc = self._parse_block(block)
                if proc:
                    results.append(proc)

        # Ещё вариант: весь HTML как текст
        if not results and len(html) > 200:
            results = self._parse_text(html)

        return results

    def _parse_row(self, cells: list) -> EnforcementProceeding | None:
        """Парсинг строки таблицы."""
        try:
            texts = [c.get_text(strip=True) for c in cells]
            proc = EnforcementProceeding()

            for i, text in enumerate(texts):
                # Номер ИП
                ip_match = re.search(r"(\d+/\d+/\d+-\w+)", text)
                if ip_match and not proc.number:
                    proc.number = ip_match.group(1)

                # Сумма
                amount_match = re.search(r"(\d[\d\s]*[.,]\d{2})", text)
                if amount_match and not proc.amount:
                    s = amount_match.group(1).replace(" ", "").replace(",", ".")
                    try:
                        proc.amount = float(s)
                    except ValueError:
                        pass

                # Дата
                date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
                if date_match and not proc.date_opened:
                    try:
                        proc.date_opened = date(
                            *map(int, reversed(date_match.group(1).split(".")))
                        )
                    except (ValueError, TypeError):
                        pass

            # Предмет — обычно длинная строка
            longest = max(texts, key=len) if texts else ""
            if len(longest) > 20:
                proc.subject = longest[:200]

            if proc.number:
                proc.status = EnforcementStatus.ACTIVE
                # Проверка на окончание
                full_text = " ".join(texts).lower()
                if "окончено" in full_text or "исполнено" in full_text:
                    proc.status = EnforcementStatus.FINISHED
                elif "приостановлено" in full_text:
                    proc.status = EnforcementStatus.SUSPENDED

                return proc

        except (IndexError, AttributeError):
            pass
        return None

    def _parse_block(self, block) -> EnforcementProceeding | None:
        """Парсинг блочного элемента."""
        text = block.get_text(separator="\n", strip=True)
        if len(text) < 20:
            return None
        return self._extract_from_text(text)

    def _parse_text(self, html: str) -> list[EnforcementProceeding]:
        """Парсинг из raw HTML текста."""
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(separator="\n", strip=True)
        results = []

        # Ищем все номера ИП
        ip_numbers = re.findall(r"(\d+/\d+/\d+-\w+)", text)
        for ip_num in ip_numbers:
            # Берём контекст вокруг номера
            idx = text.find(ip_num)
            context = text[max(0, idx - 200):idx + 500]
            proc = self._extract_from_text(context)
            if proc:
                proc.number = ip_num
                results.append(proc)

        return results

    def _extract_from_text(self, text: str) -> EnforcementProceeding | None:
        """Извлекает данные ИП из текстового фрагмента."""
        proc = EnforcementProceeding()

        # Номер
        num_match = re.search(r"(\d+/\d+/\d+-\w+)", text)
        if num_match:
            proc.number = num_match.group(1)

        # Сумма
        amount_match = re.search(r"(\d[\d\s]*[.,]\d{2})\s*(?:руб|₽)?", text)
        if amount_match:
            s = amount_match.group(1).replace(" ", "").replace(",", ".")
            try:
                proc.amount = float(s)
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
        subj_match = re.search(r"(?:Предмет|предмет)[:\s]*(.+?)(?:\n|$)", text)
        if subj_match:
            proc.subject = subj_match.group(1).strip()[:200]

        # Статус
        lower = text.lower()
        if "окончено" in lower or "исполнено" in lower:
            proc.status = EnforcementStatus.FINISHED
        elif "приостановлено" in lower:
            proc.status = EnforcementStatus.SUSPENDED
        elif proc.number:
            proc.status = EnforcementStatus.ACTIVE

        return proc if proc.number else None
