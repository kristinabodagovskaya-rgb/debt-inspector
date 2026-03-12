"""
Парсер ФССП (Федеральная служба судебных приставов).

Рабочий endpoint: https://is-go.fssp.gov.ru/ajax_search (GET, JSONP).
Капча показывается пользователю в браузере — он вводит код вручную.
"""

import asyncio
import json
import re
from datetime import date
from bs4 import BeautifulSoup

from .base import BaseSource
from debt_inspector.models.debtor import SearchParams, SubjectType
from debt_inspector.models.enforcement import EnforcementProceeding, EnforcementStatus

SEARCH_URL = "https://is-go.fssp.gov.ru/ajax_search"


class CaptchaRequired(Exception):
    """Исключение — нужна капча. Содержит данные для показа пользователю."""

    def __init__(self, captcha_image: str, code_id: str, query_params: dict):
        self.captcha_image = captcha_image  # base64 PNG
        self.code_id = code_id
        self.query_params = query_params
        super().__init__("ФССП требует ввод капчи")


class FSSPSource(BaseSource):
    name = "fssp"
    base_url = "https://fssp.gov.ru"

    async def search(self, params: SearchParams) -> list[EnforcementProceeding]:
        """Поиск исполнительных производств. Может выбросить CaptchaRequired."""
        try:
            await self._get(f"{self.base_url}/iss/ip")
            await asyncio.sleep(0.5)

            query_params = self._build_params(params)
            if not query_params:
                return []

            return await self._do_search(query_params)
        except CaptchaRequired:
            raise  # Пробрасываем наверх для показа пользователю
        except Exception as e:
            raise RuntimeError(f"ФССП: {e}") from e

    async def search_with_captcha(
        self, query_params: dict, code_id: str, captcha_code: str
    ) -> list[EnforcementProceeding]:
        """Повторный запрос с решённой капчей."""
        import sys
        await self._get(f"{self.base_url}/iss/ip")
        await asyncio.sleep(0.3)

        callback_name = "jsonp_cb"
        params = {**query_params}
        params["code_id"] = code_id
        params["code"] = captcha_code
        params["callback"] = callback_name

        print(f"[FSSP CAPTCHA] code_id={code_id} code={captcha_code} params={query_params}", file=sys.stderr)
        resp = await self._get(SEARCH_URL, params=params)
        print(f"[FSSP CAPTCHA] status={resp.status_code} len={len(resp.text)}", file=sys.stderr)
        data = self._parse_jsonp(resp.text, callback_name)

        if not data:
            print("[FSSP CAPTCHA] parse_jsonp returned None", file=sys.stderr)
            return []

        html = data.get("data", "")
        print(f"[FSSP CAPTCHA] html len={len(html)} has_captcha={'captcha-popup' in html} preview={html[:400]}", file=sys.stderr)

        if "captcha-popup" in html:
            # Капча снова — неправильный код
            img, new_code_id = self._extract_captcha(html)
            raise CaptchaRequired(img, new_code_id, query_params)

        # Сохраняем HTML для отладки (временно)
        try:
            with open("/tmp/fssp_debug.html", "w") as f:
                f.write(html)
            print(f"[FSSP CAPTCHA] saved HTML to /tmp/fssp_debug.html", file=sys.stderr)
        except Exception:
            pass

        results = self._parse_results(html)
        print(f"[FSSP CAPTCHA] parsed {len(results)} results", file=sys.stderr)
        return results

    def _build_params(self, params: SearchParams) -> dict | None:
        """Формирует параметры запроса."""
        base = {
            "system": "ip",
            "nocache": "1",
            "is[extended]": "1",
        }

        if params.region:
            base["is[region_id][0]"] = str(params.region)

        if params.subject_type == SubjectType.PERSON:
            # Для физлиц — сначала поиск по ФИО, ИНН как доп. фильтр если нет имени
            if params.last_name:
                result = {
                    **base,
                    "is[variant]": "1",
                    "is[last_name]": params.last_name,
                    "is[first_name]": params.first_name or "",
                    "is[patronymic]": params.middle_name or "",
                }
                if params.birth_date:
                    result["is[date]"] = params.birth_date
                return result
            # Нет имени — пробуем по ИНН
            if params.inn:
                return {**base, "is[variant]": "5", "is[inn]": params.inn}
            return None

        # Юрлицо: ИНН или название
        if params.inn:
            return {**base, "is[variant]": "5", "is[inn]": params.inn}

        if params.company_name:
            return {
                **base,
                "is[variant]": "2",
                "is[drtr_name]": params.company_name,
            }

        return None

    async def _do_search(self, query_params: dict) -> list[EnforcementProceeding]:
        """Выполняет запрос. При капче — выбрасывает CaptchaRequired."""
        callback_name = "jsonp_cb"
        query_params["callback"] = callback_name

        resp = await self._get(SEARCH_URL, params=query_params)
        import sys
        print(f"[FSSP DEBUG] status={resp.status_code} len={len(resp.text)}", file=sys.stderr)
        print(f"[FSSP DEBUG] raw[:500]={resp.text[:500]}", file=sys.stderr)
        data = self._parse_jsonp(resp.text, callback_name)

        if not data:
            print("[FSSP DEBUG] parse_jsonp returned None", file=sys.stderr)
            return []

        html = data.get("data", "")
        print(f"[FSSP DEBUG] html len={len(html)} preview={html[:300]}", file=sys.stderr)

        if not html or "Заполните" in html or "Выберите" in html or len(html) < 50:
            print(f"[FSSP DEBUG] html rejected: empty={not html} len={len(html)}", file=sys.stderr)
            return []

        if "captcha-popup" in html:
            img, code_id = self._extract_captcha(html)
            # Убираем callback из params перед сохранением
            saved_params = {k: v for k, v in query_params.items() if k != "callback"}
            raise CaptchaRequired(img, code_id, saved_params)

        results = self._parse_results(html)
        print(f"[FSSP DEBUG] parsed {len(results)} results", file=sys.stderr)
        return results

    def _extract_captcha(self, html: str) -> tuple[str, str]:
        """Извлекает base64 картинку и code_id из HTML капчи."""
        img_match = re.search(r'src="data:image/png;base64,([^"]+)"', html)
        captcha_image = img_match.group(1) if img_match else ""

        code_id_match = re.search(r'code_id=([^&"]+)', html)
        code_id = code_id_match.group(1) if code_id_match else ""

        return captcha_image, code_id

    def _parse_jsonp(self, text: str, callback_name: str) -> dict | None:
        """Парсит JSONP ответ."""
        text = text.strip()
        prefix = f"{callback_name}("
        if text.startswith(prefix) and text.endswith(");"):
            json_str = text[len(prefix):-2]
        elif text.startswith(prefix) and text.endswith(")"):
            json_str = text[len(prefix):-1]
        else:
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

        # Таблица
        table = soup.select_one("table") or soup.select_one(".results")
        if table:
            rows = table.select("tr")
            for row in rows:
                cells = row.select("td")
                if len(cells) >= 4:
                    proc = self._parse_row(cells)
                    if proc:
                        results.append(proc)

        # Блоки
        if not results:
            for block in soup.select(".iss-result") or soup.select("[class*='result']"):
                proc = self._parse_block(block)
                if proc:
                    results.append(proc)

        # Raw text
        if not results and len(html) > 200:
            results = self._parse_text(html)

        return results

    def _parse_row(self, cells: list) -> EnforcementProceeding | None:
        """Парсит строку таблицы ФССП (8 столбцов)."""
        try:
            if len(cells) < 6:
                return self._parse_row_generic(cells)

            proc = EnforcementProceeding()

            # Столбец 1 (idx 1): Номер ИП + дата возбуждения
            ip_text = cells[1].get_text(strip=True)
            ip_match = re.search(r"(\d+/\d+/\d+-\w+)", ip_text)
            if ip_match:
                proc.number = ip_match.group(1)
            date_match = re.search(r"от\s*(\d{2}\.\d{2}\.\d{4})", ip_text)
            if date_match:
                try:
                    proc.date_opened = date(*map(int, reversed(date_match.group(1).split("."))))
                except (ValueError, TypeError):
                    pass

            if not proc.number:
                return None

            # Столбец 2 (idx 2): Реквизиты — документ, суд, ИНН взыскателя
            doc_text = cells[2].get_text(separator="\n", strip=True)
            # ИНН взыскателя — чистое число 10 или 12 цифр
            inn_match = re.search(r"\b(\d{10}|\d{12})\b", doc_text)
            proc.claimant = inn_match.group(1) if inn_match else ""

            # Столбец 3 (idx 3): Дата окончания, основание
            end_text = cells[3].get_text(separator=" ", strip=True)
            if end_text.strip():
                proc.termination_reason = end_text.strip()[:100]

            # Столбец 4 (idx 4): Сервис — debt-rest атрибут
            debt_rest_tag = cells[4].select_one("[debt-rest]")
            debt_rest = float(debt_rest_tag["debt-rest"]) if debt_rest_tag else None

            # Столбец 5 (idx 5): Предмет + сумма долга
            subj_text = cells[5].get_text(separator="\n", strip=True)
            subj_lines = [l.strip() for l in subj_text.split("\n") if l.strip()]
            # Первая строка = тип долга
            if subj_lines:
                proc.subject = subj_lines[0][:200]
            # Сумма долга
            amount_match = re.search(r"Сумма долга:\s*([\d\s]+[.,]\d{2})", subj_text)
            if amount_match:
                s = amount_match.group(1).replace(" ", "").replace(",", ".")
                try:
                    proc.amount = float(s)
                except ValueError:
                    pass
            # Fallback на debt-rest
            if not proc.amount and debt_rest:
                proc.amount = debt_rest

            # Столбец 6 (idx 6): Отдел приставов
            if len(cells) > 6:
                proc.department = cells[6].get_text(strip=True)[:100]

            # Столбец 7 (idx 7): Пристав
            if len(cells) > 7:
                proc.bailiff = cells[7].get_text(strip=True)[:100]

            # Статус: есть дата окончания → окончено, иначе активно
            if proc.termination_reason and re.search(r"\d{2}\.\d{2}\.\d{4}", proc.termination_reason):
                lower = proc.termination_reason.lower()
                if "ст. 46" in lower or "ст.46" in lower:
                    proc.status = EnforcementStatus.FINISHED
                elif "приостановлено" in lower:
                    proc.status = EnforcementStatus.SUSPENDED
                else:
                    proc.status = EnforcementStatus.FINISHED
            else:
                proc.status = EnforcementStatus.ACTIVE

            return proc
        except (IndexError, AttributeError):
            pass
        return None

    def _parse_row_generic(self, cells: list) -> EnforcementProceeding | None:
        """Fallback парсер для нестандартных таблиц."""
        try:
            texts = [c.get_text(strip=True) for c in cells]
            proc = EnforcementProceeding()

            for text in texts:
                ip_match = re.search(r"(\d+/\d+/\d+-\w+)", text)
                if ip_match and not proc.number:
                    proc.number = ip_match.group(1)

                text_no_dates = re.sub(r"\d{2}\.\d{2}\.\d{4}", "", text)
                amount_match = re.search(r"(\d[\d\s]*[.,]\d{2})", text_no_dates)
                if amount_match and not proc.amount:
                    s = amount_match.group(1).replace(" ", "").replace(",", ".")
                    try:
                        proc.amount = float(s)
                    except ValueError:
                        pass

            if proc.number:
                full_text = " ".join(texts).lower()
                if "окончено" in full_text or "исполнено" in full_text:
                    proc.status = EnforcementStatus.FINISHED
                else:
                    proc.status = EnforcementStatus.ACTIVE
                return proc
        except (IndexError, AttributeError):
            pass
        return None

    def _parse_block(self, block) -> EnforcementProceeding | None:
        text = block.get_text(separator="\n", strip=True)
        if len(text) < 20:
            return None
        return self._extract_from_text(text)

    def _parse_text(self, html: str) -> list[EnforcementProceeding]:
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(separator="\n", strip=True)
        results = []

        for ip_num in re.findall(r"(\d+/\d+/\d+-\w+)", text):
            idx = text.find(ip_num)
            context = text[max(0, idx - 200):idx + 500]
            proc = self._extract_from_text(context)
            if proc:
                proc.number = ip_num
                results.append(proc)

        return results

    def _extract_from_text(self, text: str) -> EnforcementProceeding | None:
        proc = EnforcementProceeding()

        num_match = re.search(r"(\d+/\d+/\d+-\w+)", text)
        if num_match:
            proc.number = num_match.group(1)

        # Убираем даты перед поиском суммы
        text_no_dates = re.sub(r"\d{2}\.\d{2}\.\d{4}", "", text)
        amount_match = re.search(r"(\d[\d\s]*[.,]\d{2})\s*(?:руб|₽)?", text_no_dates)
        if amount_match:
            s = amount_match.group(1).replace(" ", "").replace(",", ".")
            try:
                proc.amount = float(s)
            except ValueError:
                pass

        date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
        if date_match:
            try:
                proc.date_opened = date(
                    *map(int, reversed(date_match.group(1).split(".")))
                )
            except (ValueError, TypeError):
                pass

        subj_match = re.search(r"(?:Предмет|предмет)[:\s]*(.+?)(?:\n|$)", text)
        if subj_match:
            proc.subject = subj_match.group(1).strip()[:200]

        lower = text.lower()
        if "окончено" in lower or "исполнено" in lower:
            proc.status = EnforcementStatus.FINISHED
        elif "приостановлено" in lower:
            proc.status = EnforcementStatus.SUSPENDED
        elif proc.number:
            proc.status = EnforcementStatus.ACTIVE

        return proc if proc.number else None
