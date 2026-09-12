"""Шаблоны уведомлений нотификатора."""

PAYMENT_TEMPLATE = "Платёж {payment_id} на сумму {amount} проведён."


def render_payment(payment_id: int, amount: float) -> str:
    return PAYMENT_TEMPLATE.format(payment_id=payment_id, amount=amount)
