"""Deterministic texts: used when the `client_agent` is unavailable or its output is invalid.

Nothing here promises prices, deadlines or results. Owner alerts are in the owner's language.
"""

FALLBACK: dict[str, dict[str, str]] = {
    "es": {
        "acknowledgement": (
            "Recibido, gracias por avisar. Lo estamos revisando y te escribimos en cuanto "
            "tengamos novedades."
        ),
        "info_request": (
            "Gracias por escribir. Para ayudarte mejor, ¿puedes contarnos un poco más sobre lo "
            "que pasa y en qué pantalla ocurre?"
        ),
        "status_update": (
            "Gracias por el mensaje. Seguimos con tu caso y te avisamos en cuanto haya novedades."
        ),
        "resolution_notice": (
            "Listo, ya corregimos el problema que reportaste. Puedes probar de nuevo y avisarnos "
            "si algo sigue fallando."
        ),
    },
    "en": {
        "acknowledgement": (
            "Got it, thanks for letting us know. We're looking into it and will get back to you "
            "as soon as we have news."
        ),
        "info_request": (
            "Thanks for reaching out. To help you better, could you tell us a bit more about "
            "what happens and on which screen?"
        ),
        "status_update": (
            "Thanks for your message. We're still on your case and will let you know as soon "
            "as there's news."
        ),
        "resolution_notice": (
            "Done, we fixed the problem you reported. You can try again and let us know if "
            "anything is still off."
        ),
    },
}


def fallback(kind: str, language: str) -> str:
    table = FALLBACK.get(language, FALLBACK["es"])
    return table.get(kind, table["acknowledgement"])
