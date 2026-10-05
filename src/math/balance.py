"""Lectura estricta del cash de Kalshi, sin red ni redondeo a centavos."""

from decimal import Decimal, InvalidOperation


def available_cash_usd(payload: object) -> Decimal:
    """Dólares fixed-point primero; centavos legacy sólo si no existe el campo nuevo.

    Una respuesta inválida no es saldo cero y no renueva la frescura del riesgo.
    Mensajes seguros: nunca incorporar el cuerpo HTTP a errores o logs.
    """
    if not isinstance(payload, dict):
        raise ValueError("Balance Kalshi: objeto requerido")
    cents = None
    if "balance" in payload:
        legacy = payload["balance"]
        if type(legacy) is not int or legacy < 0 or legacy > 100_000_000_000:
            raise ValueError("Balance Kalshi: centavos inválidos")
        cents = Decimal(legacy) / 100
    if "balance_dollars" not in payload:
        if cents is None:
            raise ValueError("Balance Kalshi: efectivo ausente")
        return cents
    raw = payload["balance_dollars"]
    if not isinstance(raw, str) or not raw or len(raw) > 64:
        raise ValueError("Balance Kalshi: dólares inválidos")
    try:
        dollars = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Balance Kalshi: dólares inválidos") from exc
    if (
        not dollars.is_finite()
        or dollars < 0
        or dollars > Decimal("1000000000")
        or dollars.as_tuple().exponent < -4
    ):
        raise ValueError("Balance Kalshi: dólares fuera de rango")
    # El entero legacy pierde subcentavos; discrepancias de un centavo completo
    # o más no se resuelven eligiendo silenciosamente el campo de mayor saldo.
    if cents is not None and abs(dollars - cents) >= Decimal("0.01"):
        raise ValueError("Balance Kalshi: campos monetarios contradictorios")
    return dollars
