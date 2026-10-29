from decimal import ROUND_CEILING, Decimal

from app import settings_store as st


def effective_rate() -> Decimal:
    """Курс USD→RUB с надбавкой."""
    return st.dec("usd_rate") * (1 + st.dec("usd_rate_extra") / 100)


def sale_price(cost_usd: Decimal) -> Decimal:
    """Цена продажи в рублях, округлённая вверх до целого."""
    cost_rub = cost_usd * effective_rate()
    if st.raw("markup_type") == "fixed":
        price = cost_rub + st.dec("markup_fixed")
    else:
        markup = max(cost_rub * st.dec("markup_percent") / 100, st.dec("markup_min"))
        if st.dec("markup_max") > 0:  # потолок наценки; минимальная всё равно важнее
            markup = max(min(markup, st.dec("markup_max")), st.dec("markup_min"))
        price = cost_rub + markup
    return price.quantize(Decimal("1"), rounding=ROUND_CEILING)
