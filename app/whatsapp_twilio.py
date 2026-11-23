"""Envío de WhatsApp por Twilio con plantilla aprobada (número de reportes).

Usa las mismas credenciales y el mismo remitente (TWILIO_WHATSAPP_FROM) que el
reporte diario de dirección. NO es el bot de ventas de Meta.
"""
import json
import os
import re


def _destino_whatsapp(numero_10_digitos: str) -> str:
    """10 dígitos de México → 'whatsapp:+521XXXXXXXXXX' (mismo formato que REPORTE_WHATSAPP_TO)."""
    digitos = re.sub(r"\D", "", numero_10_digitos or "")
    if len(digitos) != 10:
        raise ValueError(f"Número inválido: se esperaban 10 dígitos y llegaron {len(digitos)}")
    return f"whatsapp:+521{digitos}"


def enviar_whatsapp_plantilla(numero_10_digitos: str, content_sid: str, variables_dict: dict) -> str:
    """Manda una plantilla de Twilio a un número de 10 dígitos. Devuelve el SID del mensaje.
    Lanza excepción si falta configuración, el número es inválido o Twilio falla."""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_number = os.environ.get("TWILIO_WHATSAPP_FROM", "")
    if not account_sid or not auth_token or not from_number:
        raise RuntimeError("Faltan TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM")
    if not content_sid:
        raise RuntimeError("Falta el content_sid de la plantilla")

    to = _destino_whatsapp(numero_10_digitos)

    from twilio.rest import Client
    client = Client(account_sid, auth_token)
    msg = client.messages.create(
        from_=from_number,
        to=to,
        content_sid=content_sid,
        content_variables=json.dumps({str(k): str(v) for k, v in variables_dict.items()}),
    )
    return msg.sid
