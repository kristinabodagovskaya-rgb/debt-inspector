"""
AI-чат помощник по банкротству физических лиц.

Использует OpenAI GPT для ответов на вопросы пользователей
о процедуре банкротства на каждом шаге wizard.
"""

import os
import httpx

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

SYSTEM_PROMPT = """Ты — юридический помощник по банкротству физических лиц в России.
Отвечай кратко, по существу, на русском языке.
Ссылайся на конкретные статьи ФЗ-127 «О несостоятельности (банкротстве)».

Ключевые факты:
- Судебное банкротство: долг от 500 000 руб., госпошлина 300 руб., депозит 25 000 руб.
- Внесудебное через МФЦ: долг 25 000 - 1 000 000 руб., бесплатно, 6 месяцев.
- Для МФЦ нужно: окончание ИП по ст. 46, ИЛИ ИП > 7 лет, ИЛИ пенсионер/получатель пособий.
- Ходатайство о пропуске реструктуризации (ст. 213.6 п.8) — если доход < прожиточного минимума.
- Подача через my.arbitr.ru ускоряет процесс на 2-4 недели.
- Не списываются: алименты, вред здоровью, субсидиарная ответственность.
- Единственное жильё не подлежит реализации (кроме ипотечного).

Не давай юридических консультаций — рекомендуй обратиться к юристу для сложных случаев.
"""

STEP_CONTEXTS = {
    "search": "Пользователь на шаге поиска долгов (ФССП, суды, ЕФРСБ). Помоги разобраться в результатах поиска.",
    "assessment": "Пользователь на шаге оценки ситуации. Помоги выбрать маршрут (МФЦ vs суд) и понять рекомендации по ускорению.",
    "profile": "Пользователь заполняет анкету (паспорт, СНИЛС, адрес, имущество). Помоги с заполнением.",
    "creditors": "Пользователь составляет список кредиторов. Помоги определить какие долги включать.",
    "documents": "Пользователь собирает документы для банкротства. Помоги разобраться где получить нужные справки.",
    "application": "Пользователь на шаге генерации заявления. Помоги проверить корректность данных.",
    "payment": "Пользователь на шаге оплаты госпошлины и депозита. Помоги с оплатой и реквизитами.",
    "filing": "Пользователь готовится подать заявление. Помоги с выбором СРО и способа подачи.",
}


async def chat(question: str, step: str = "", context: str = "") -> str:
    """Отправляет вопрос в OpenAI и возвращает ответ."""
    if not OPENAI_API_KEY:
        return "AI-помощник недоступен: не задан OPENAI_API_KEY. Добавьте ключ в переменные окружения."

    step_context = STEP_CONTEXTS.get(step, "")
    system = SYSTEM_PROMPT
    if step_context:
        system += f"\n\nТекущий шаг: {step_context}"
    if context:
        system += f"\n\nКонтекст пользователя: {context}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{OPENAI_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": OPENAI_MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": question},
                    ],
                    "max_tokens": 800,
                    "temperature": 0.3,
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as e:
        return f"Ошибка API: {e.response.status_code}"
    except Exception as e:
        return f"Ошибка: {str(e)}"
