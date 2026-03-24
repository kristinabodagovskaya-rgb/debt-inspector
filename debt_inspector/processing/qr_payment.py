"""
Генерация QR-кодов для оплаты госпошлины и депозита.

Формат: стандартные банковские реквизиты для перевода.
QR возвращается как base64 PNG для встраивания в HTML.
"""

import base64
import io

try:
    import segno
    HAS_SEGNO = True
except ImportError:
    HAS_SEGNO = False


def _build_payment_string(
    recipient: str,
    inn: str,
    kpp: str,
    account: str,
    bank: str,
    bik: str,
    kbk: str,
    oktmo: str,
    amount: float,
    purpose: str,
    payer_name: str = "",
    payer_inn: str = "",
) -> str:
    """Формирует строку для QR по формату банковского перевода.

    Используется упрощённый формат — реквизиты + назначение платежа,
    считываемый мобильными банковскими приложениями.
    """
    lines = [
        f"Получатель: {recipient}",
        f"ИНН: {inn}",
        f"КПП: {kpp}",
        f"Р/с: {account}",
        f"Банк: {bank}",
        f"БИК: {bik}",
        f"КБК: {kbk}",
        f"ОКТМО: {oktmo}",
        f"Сумма: {amount:.2f} руб.",
        f"Назначение: {purpose}",
    ]
    if payer_name:
        lines.append(f"Плательщик: {payer_name}")
    if payer_inn:
        lines.append(f"ИНН плательщика: {payer_inn}")
    return "\n".join(lines)


def generate_fee_qr(court_details: dict, payer_name: str = "", payer_inn: str = "") -> str | None:
    """Генерирует QR-код для оплаты госпошлины 300 руб.

    Возвращает base64-encoded PNG или None если segno не установлен.
    """
    if not HAS_SEGNO or not court_details:
        return None

    text = _build_payment_string(
        recipient=court_details.get("fee_recipient", ""),
        inn=court_details.get("fee_inn", ""),
        kpp=court_details.get("fee_kpp", ""),
        account=court_details.get("fee_account", ""),
        bank=court_details.get("fee_bank", ""),
        bik=court_details.get("fee_bik", ""),
        kbk=court_details.get("fee_kbk", ""),
        oktmo=court_details.get("fee_oktmo", ""),
        amount=300.0,
        purpose="Госпошлина за подачу заявления о банкротстве гражданина (ст. 333.21 НК РФ)",
        payer_name=payer_name,
        payer_inn=payer_inn,
    )
    return _qr_to_base64(text)


def generate_deposit_qr(court_details: dict, payer_name: str = "", payer_inn: str = "") -> str | None:
    """Генерирует QR-код для внесения депозита 25 000 руб.

    Возвращает base64-encoded PNG или None если segno не установлен.
    """
    if not HAS_SEGNO or not court_details:
        return None

    text = _build_payment_string(
        recipient=court_details.get("deposit_recipient", ""),
        inn=court_details.get("fee_inn", ""),
        kpp=court_details.get("fee_kpp", ""),
        account=court_details.get("deposit_account", ""),
        bank=court_details.get("deposit_bank", ""),
        bik=court_details.get("deposit_bik", ""),
        kbk="",
        oktmo="",
        amount=25_000.0,
        purpose=court_details.get("deposit_purpose", "Депозит на вознаграждение финансового управляющего"),
        payer_name=payer_name,
        payer_inn=payer_inn,
    )
    return _qr_to_base64(text)


def _qr_to_base64(text: str) -> str:
    """Создаёт QR-код и возвращает как base64 PNG."""
    qr = segno.make(text)
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=5, border=2)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")
