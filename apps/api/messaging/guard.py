"""Deterministic content guard of the `OutboundPolicy` (permissions-and-approvals.md).

The `client_agent` is told not to commit to prices, deadlines or contractual terms; this guard
does not trust it. Any hit reclassifies the message as `commit_commercial_terms`, which is
critical and always needs the owner's approval. It is conservative on purpose: a false positive
costs one approval, a false negative costs a promise Jarvis cannot keep.
"""

import re

_PATTERNS: dict[str, re.Pattern[str]] = {
    "price": re.compile(
        r"(?:(?:US|RD|MX|AR|CO)\s?\$|\$|€|£)\s?\d"
        r"|\d\s?(?:USD|EUR|DOP|MXN|COP|ARS)\b"
        r"|\b\d[\d.,]*\s?(?:d[oó]lares|pesos|euros|dollars)\b"
        r"|\b(?:precio|precios|tarifa|tarifas|cotizaci[oó]n|presupuesto|descuento|gratis|"
        r"sin costo|sin coste|reembolso|devoluci[oó]n del dinero|factura|"
        r"price|prices|pricing|quote|quotation|discount|free of charge|refund|invoice)\b",
        re.IGNORECASE,
    ),
    "deadline": re.compile(
        r"\b(?:en|dentro de|antes de(?:l)?|para (?:el|este|esta)|hoy mismo|esta semana|"
        r"este mes|in|within|by|before)\b"
        r"[^.\n]{0,20}?"
        r"\b(?:\d+|un|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|one|two|three|"
        r"four|five|six|seven|eight|nine|ten)?\s?"
        r"(?:d[ií]as?|semanas?|horas?|meses?|days?|weeks?|hours?|months?|"
        r"lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|ma[ñn]ana|tomorrow|tonight)\b",
        re.IGNORECASE,
    ),
    "contract": re.compile(
        r"\b(?:contrato|contratos|cl[aá]usula|garantizamos|garantizo|garant[ií]a|compensaci[oó]n|"
        r"indemnizaci[oó]n|penalizaci[oó]n|alcance del contrato|"
        r"contract|contracts|clause|we guarantee|guarantee|warranty|compensation|penalty)\b",
        re.IGNORECASE,
    ),
}


def commercial_terms(text: str) -> list[str]:
    """Categories of commercial commitment found in `text` (empty when none)."""
    return [name for name, pattern in _PATTERNS.items() if pattern.search(text)]
