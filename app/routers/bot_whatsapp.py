"""Bot de ventas por WhatsApp (numero 449 102 7916, Meta Cloud API).

- GET  /bot-whatsapp/webhook  -> verificacion del webhook (Meta).
- POST /bot-whatsapp/webhook  -> mensajes entrantes y estados de envio.

Solo atiende el numero cuyo phone_number_id esta en BOT_WA_PHONE_NUMBER_ID.
Cualquier otro numero (por ejemplo el de reportes) se ignora.

Tablas (creadas a mano en Supabase, NO son modelos SQLAlchemy para no
pasar por create_all): bot_conversaciones, bot_mensajes, bot_config.

Variables de entorno (Render):
  BOT_WA_TOKEN            token permanente del usuario del sistema ATOBotVentas
  BOT_WA_PHONE_NUMBER_ID  identificador del 449 102 7916 (1493497883841826)
  BOT_WA_VERIFY_TOKEN     texto que se escribe tambien en Meta al configurar el webhook
  BOT_WA_APP_SECRET       clave secreta de la app (para validar la firma de Meta)
  BOT_ANTHROPIC_API_KEY   clave "ATO Bot Ventas" (si falta, usa ANTHROPIC_API_KEY)
"""

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

import anthropic
import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from app.bot_prompt import PROMPT_BASE, TOOL_RESPONDER
from app.database import SessionLocal

log = logging.getLogger("uvicorn.error")  # asi los errores salen en el log de Render

ULTIMO_ERROR_ENVIO = {"texto": None}

router = APIRouter()

GRAPH_URL = "https://graph.facebook.com/v22.0"
ESPERA_AGRUPAR_SEG = 8      # espera para juntar varios mensajes seguidos del cliente
MAX_HISTORIAL = 40          # mensajes que se le pasan a Claude
MODELO_DEFAULT = "claude-haiku-4-5"

IMAGENES_PLANES = {
    "libre_400": "img_libre_400",
    "libre_lineanueva": "img_libre_lineanueva",
    "libre_portabilidad": "img_libre_portabilidad",
}


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

def _env(nombre: str) -> str:
    return (os.getenv(nombre) or "").strip()


def _leer_config(db) -> dict:
    filas = db.execute(text("SELECT clave, valor FROM bot_config")).fetchall()
    return {f[0]: f[1] for f in filas}


def _firma_valida(cuerpo: bytes, firma: Optional[str]) -> bool:
    secreto = _env("BOT_WA_APP_SECRET")
    if not secreto:
        # Sin secreto no se puede validar que el mensaje venga de Meta: se rechaza.
        log.error("BOT_WA_APP_SECRET no configurado: se rechazan los mensajes del webhook")
        return False
    if not firma or not firma.startswith("sha256="):
        return False
    esperado = hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, firma.split("=", 1)[1])


def _texto_de_mensaje(msg: dict) -> Optional[str]:
    """Convierte un mensaje de WhatsApp a texto para el historial.
    Regresa None si no amerita respuesta (reacciones, etc.)."""
    tipo = msg.get("type")
    if tipo == "text":
        texto = (msg.get("text") or {}).get("body", "")
    elif tipo == "image":
        cap = (msg.get("image") or {}).get("caption")
        texto = "[El cliente envió una foto]" + (f" {cap}" if cap else "")
    elif tipo == "document":
        cap = (msg.get("document") or {}).get("caption")
        texto = "[El cliente envió un documento]" + (f" {cap}" if cap else "")
    elif tipo == "audio":
        texto = "[El cliente envió un audio; pídele amable que lo escriba por mensaje]"
    elif tipo == "video":
        texto = "[El cliente envió un video]"
    elif tipo == "sticker":
        texto = "[El cliente envió un sticker]"
    elif tipo == "location":
        loc = msg.get("location") or {}
        texto = f"[El cliente envió su ubicación: {loc.get('name') or ''} {loc.get('address') or ''}]".strip()
    elif tipo == "button":
        texto = (msg.get("button") or {}).get("text", "")
    elif tipo == "interactive":
        inter = msg.get("interactive") or {}
        texto = (inter.get("button_reply") or inter.get("list_reply") or {}).get("title", "")
    elif tipo == "reaction":
        return None
    else:
        texto = f"[El cliente envió un mensaje de tipo {tipo}]"

    ref = msg.get("referral")
    if ref:
        anuncio = ref.get("headline") or ref.get("body") or ""
        texto = f"[Llegó desde el anuncio: {anuncio[:200]}] " + texto
    return texto


def _media_id(msg: dict) -> Optional[str]:
    tipo = msg.get("type")
    if tipo in ("image", "document", "audio", "video", "sticker"):
        return (msg.get(tipo) or {}).get("id")
    return None


def _enviar(payload: dict) -> Optional[str]:
    """Envia a la API de WhatsApp. Regresa el id del mensaje o None."""
    token = _env("BOT_WA_TOKEN")
    phone_id = _env("BOT_WA_PHONE_NUMBER_ID")
    ULTIMO_ERROR_ENVIO["texto"] = None
    if not token or not phone_id:
        ULTIMO_ERROR_ENVIO["texto"] = "error: faltan BOT_WA_TOKEN o BOT_WA_PHONE_NUMBER_ID"
        log.error("[bot-whatsapp] Faltan BOT_WA_TOKEN o BOT_WA_PHONE_NUMBER_ID")
        return None
    try:
        r = requests.post(
            f"{GRAPH_URL}/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"messaging_product": "whatsapp", **payload},
            timeout=20,
        )
        if r.status_code >= 300:
            ULTIMO_ERROR_ENVIO["texto"] = f"error {r.status_code}: {r.text[:300]}"
            log.error("[bot-whatsapp] Error al enviar WhatsApp %s: %s", r.status_code, r.text[:500])
            return None
        return ((r.json().get("messages") or [{}])[0]).get("id")
    except Exception as e:
        ULTIMO_ERROR_ENVIO["texto"] = f"error: {e}"[:300]
        log.exception("[bot-whatsapp] Excepcion al enviar WhatsApp")
        return None


def enviar_texto(wa_id: str, texto: str) -> Optional[str]:
    return _enviar({"to": wa_id, "type": "text", "text": {"body": texto, "preview_url": False}})


def enviar_imagen(wa_id: str, url: str) -> Optional[str]:
    return _enviar({"to": wa_id, "type": "image", "image": {"link": url}})


def marcar_leido(wa_message_id: str) -> None:
    _enviar({"status": "read", "message_id": wa_message_id})


def guardar_saliente(db, conv_id: int, wa_message_id: Optional[str], autor: str,
                     tipo: str, texto: str) -> None:
    db.execute(
        text("""
            INSERT INTO bot_mensajes (conversacion_id, wa_message_id, direccion, autor, tipo, texto, estado_envio)
            VALUES (:c, :w, 'saliente', :a, :t, :x, :e)
            ON CONFLICT (wa_message_id) DO NOTHING
        """),
        {"c": conv_id, "w": wa_message_id, "a": autor, "t": tipo, "x": texto,
         "e": "enviado" if wa_message_id else (ULTIMO_ERROR_ENVIO["texto"] or "error")},
    )


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------

def _historial_para_claude(db, conv_id: int) -> list:
    filas = db.execute(
        text("""
            SELECT autor, texto FROM (
                SELECT id, autor, texto FROM bot_mensajes
                WHERE conversacion_id = :c AND texto IS NOT NULL AND texto <> ''
                  AND (direccion = 'entrante' OR estado_envio IS NULL OR estado_envio NOT LIKE 'error%')
                ORDER BY id DESC LIMIT :n
            ) t ORDER BY id ASC
        """),
        {"c": conv_id, "n": MAX_HISTORIAL},
    ).fetchall()

    mensajes: list = []
    for autor, texto in filas:
        rol = "user" if autor == "cliente" else "assistant"
        if mensajes and mensajes[-1]["role"] == rol:
            mensajes[-1]["content"] += "\n" + texto
        else:
            mensajes.append({"role": rol, "content": texto})
    # La conversacion para Claude debe empezar con el cliente.
    while mensajes and mensajes[0]["role"] != "user":
        mensajes.pop(0)
    return mensajes


def _equipos_en_existencia(db) -> str:
    """Telefonos TELCEL disponibles hoy (por IMEI), con su precio de contado del sistema."""
    try:
        filas = db.execute(text("""
            SELECT e.producto, COUNT(*) AS piezas, MAX(g.precio) AS precio
            FROM equipos_telcel e
            LEFT JOIN inventario_general g ON UPPER(TRIM(g.clave)) = UPPER(TRIM(e.clave))
            WHERE e.estatus IN ('surtido', 'en_bodega')
              AND UPPER(e.producto) LIKE 'TELEFONO TELCEL%'
            GROUP BY e.producto
            ORDER BY e.producto
        """)).fetchall()
    except Exception:
        db.rollback()
        log.exception("[bot-whatsapp] No se pudo leer el inventario de equipos")
        return ""
    lineas = []
    for producto, piezas, precio in filas:
        nombre = producto.upper().replace("TELEFONO TELCEL", "").strip()
        precio_txt = f"${float(precio):,.0f}" if precio else "precio por confirmar"
        lineas.append(f"- {nombre}: contado {precio_txt}")
    return "\n".join(lineas)


def _prompt_sistema(cfg: dict, paso: int, datos: dict, equipos: str = "") -> str:
    partes = [PROMPT_BASE]
    if cfg.get("ofertas"):
        partes.append("## Ofertas vigentes / promoción (es lo PRIMERO que ofreces; úsalas tal cual, no inventes otras)\n" + cfg["ofertas"])
    if equipos:
        partes.append(
            "## Otros equipos Telcel en existencia hoy (inventario real de ATO)\n"
            "PRIORIDAD: siempre ofrece primero el equipo de la promoción vigente (sección \"Ofertas vigentes\"). "
            "NO menciones esta lista por tu cuenta. Úsala SOLO si el cliente pide ver otros modelos, pregunta por un modelo en específico, "
            "o dice que el de la promoción no le interesa. Aun así, recuérdale amable la promoción.\n"
            "Si pregunta por un modelo que no está en la lista, di que por ahora no lo tenemos y ofrece la promoción o uno parecido.\n"
            "- Precio de contado: dalo SOLO si el cliente pregunta por contado o por cuánto cuesta el equipo de contado.\n"
            "- En plan: el pago inicial y la mensualidad dependen de la evaluación de Telcel; no des cifras, se las manda el asesor en su cotización.\n"
            "- Colores: no los tenemos en sistema; dile que el asesor le confirma colores disponibles.\n"
            "- También se pueden llevar en abonos con Paguitos o PayJoy en nuestros módulos.\n"
            + equipos
        )
    if cfg.get("preguntas_frecuentes"):
        partes.append("## Preguntas frecuentes (responde así)\n" + cfg["preguntas_frecuentes"])
    if cfg.get("reglas_extra"):
        partes.append("## Reglas extra de ATO (tienen prioridad)\n" + cfg["reglas_extra"])
    partes.append(
        "## Ficha actual del cliente\n"
        f"Paso actual: {paso}\n"
        f"Datos que ya dio: {json.dumps(datos, ensure_ascii=False)}"
    )
    return "\n\n".join(partes)


def pedir_respuesta_a_claude(cfg: dict, historial: list, paso: int, datos: dict,
                             equipos: str = "") -> Optional[dict]:
    api_key = _env("BOT_ANTHROPIC_API_KEY") or _env("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("Falta BOT_ANTHROPIC_API_KEY")
        return None
    cliente = anthropic.Anthropic(api_key=api_key, timeout=60)
    resp = cliente.messages.create(
        model=cfg.get("modelo") or MODELO_DEFAULT,
        max_tokens=1000,
        system=_prompt_sistema(cfg, paso, datos, equipos),
        tools=[TOOL_RESPONDER],
        tool_choice={"type": "tool", "name": "responder"},
        messages=historial,
    )
    for bloque in resp.content:
        if getattr(bloque, "type", None) == "tool_use" and bloque.name == "responder":
            return bloque.input
    return None


# --------------------------------------------------------------------------
# Proceso de un mensaje entrante (corre en segundo plano)
# --------------------------------------------------------------------------

def registrar_entrante(wa_id: str, nombre: Optional[str], msg: dict) -> Optional[int]:
    """Guarda el mensaje. Regresa el id de la fila nueva, o None si ya existia."""
    wa_message_id = msg.get("id")
    texto = _texto_de_mensaje(msg)
    db = SessionLocal()
    try:
        ya = db.execute(text("SELECT 1 FROM bot_mensajes WHERE wa_message_id = :w"),
                        {"w": wa_message_id}).first()
        if ya:
            return None
        conv_id = db.execute(
            text("""
                INSERT INTO bot_conversaciones (wa_id, nombre_perfil, no_leidos)
                VALUES (:wa, :n, 1)
                ON CONFLICT (wa_id) DO UPDATE SET
                    nombre_perfil = COALESCE(EXCLUDED.nombre_perfil, bot_conversaciones.nombre_perfil),
                    no_leidos = bot_conversaciones.no_leidos + 1,
                    estado = CASE WHEN bot_conversaciones.estado = 'cerrado' THEN 'bot'
                                  ELSE bot_conversaciones.estado END,
                    ultimo_mensaje_at = NOW(),
                    updated_at = NOW()
                RETURNING id
            """),
            {"wa": wa_id, "n": nombre},
        ).scalar()
        msg_id = db.execute(
            text("""
                INSERT INTO bot_mensajes (conversacion_id, wa_message_id, direccion, autor, tipo, texto, media_id)
                VALUES (:c, :w, 'entrante', 'cliente', :t, :x, :m)
                ON CONFLICT (wa_message_id) DO NOTHING
                RETURNING id
            """),
            {"c": conv_id, "w": wa_message_id, "t": msg.get("type") or "text",
             "x": texto, "m": _media_id(msg)},
        ).scalar()
        db.commit()
        if texto is None:  # reacciones: se guardan pero no se contestan
            return None
        return msg_id
    except Exception:
        db.rollback()
        log.exception("Error al registrar mensaje entrante")
        return None
    finally:
        db.close()


def responder_si_toca(msg_id: int, wa_message_id: str) -> None:
    # Espera unos segundos por si el cliente manda varios mensajes seguidos;
    # solo contesta el ultimo.
    time.sleep(ESPERA_AGRUPAR_SEG)
    db = SessionLocal()
    try:
        fila = db.execute(
            text("""
                SELECT c.id, c.wa_id, c.estado, c.paso, c.datos,
                       (SELECT MAX(id) FROM bot_mensajes m
                         WHERE m.conversacion_id = c.id AND m.direccion = 'entrante') AS ultimo_entrante,
                       (SELECT MAX(id) FROM bot_mensajes m
                         WHERE m.conversacion_id = c.id AND m.direccion = 'saliente') AS ultimo_saliente
                FROM bot_conversaciones c
                JOIN bot_mensajes m0 ON m0.conversacion_id = c.id AND m0.id = :mid
            """),
            {"mid": msg_id},
        ).mappings().first()
        if not fila:
            return
        if fila["ultimo_entrante"] != msg_id:
            return  # llego otro mensaje despues; ese se encarga
        if fila["ultimo_saliente"] and fila["ultimo_saliente"] > msg_id:
            return  # alguien ya contesto (asesor)
        if fila["estado"] != "bot":
            return

        cfg = _leer_config(db)
        if (cfg.get("bot_activo") or "").strip().lower() != "true":
            return

        conv_id = fila["id"]
        wa_id = fila["wa_id"]
        datos = fila["datos"] or {}
        historial = _historial_para_claude(db, conv_id)
        if not historial:
            return

        equipos = _equipos_en_existencia(db)
        resultado = pedir_respuesta_a_claude(cfg, historial, fila["paso"], datos, equipos)
        if not resultado:
            return

        marcar_leido(wa_message_id)

        mensajes = [m for m in (resultado.get("mensajes") or []) if m and m.strip()][:2]
        for i, texto_msg in enumerate(mensajes):
            wid = enviar_texto(wa_id, texto_msg.strip())
            guardar_saliente(db, conv_id, wid, "bot", "text", texto_msg.strip())
            if i == 0:
                img = resultado.get("imagen_planes") or "ninguna"
                url = cfg.get(IMAGENES_PLANES.get(img, ""), "") if img != "ninguna" else ""
                if url:
                    wid_img = enviar_imagen(wa_id, url)
                    guardar_saliente(db, conv_id, wid_img, "bot", "image", f"[Imagen de planes: {img}]")

        nuevos = {k: v for k, v in (resultado.get("datos") or {}).items() if v not in (None, "")}
        pasar = bool(resultado.get("pasar_a_asesor"))
        motivo = resultado.get("motivo") or "ninguno"
        if pasar:
            nuevos["motivo_asesor"] = motivo
        db.execute(
            text("""
                UPDATE bot_conversaciones SET
                    paso = :paso,
                    datos = datos || CAST(:nuevos AS jsonb),
                    estado = CASE WHEN :pasar THEN 'asesor' ELSE estado END,
                    datos_completos = datos_completos OR :completos,
                    no_leidos = CASE WHEN :pasar THEN no_leidos ELSE 0 END,
                    ultimo_mensaje_at = NOW(),
                    updated_at = NOW()
                WHERE id = :c
            """),
            {"paso": int(resultado.get("paso") or fila["paso"]),
             "nuevos": json.dumps(nuevos, ensure_ascii=False),
             "pasar": pasar,
             "completos": motivo == "datos_completos",
             "c": conv_id},
        )
        db.commit()
    except Exception:
        db.rollback()
        log.exception("Error al responder con el bot")
    finally:
        db.close()


def procesar_entrante(wa_id: str, nombre: Optional[str], msg: dict) -> None:
    msg_id = registrar_entrante(wa_id, nombre, msg)
    if msg_id:
        responder_si_toca(msg_id, msg.get("id"))


def actualizar_estado(status: dict) -> None:
    db = SessionLocal()
    try:
        db.execute(
            text("UPDATE bot_mensajes SET estado_envio = :e WHERE wa_message_id = :w"),
            {"e": status.get("status"), "w": status.get("id")},
        )
        db.commit()
    except Exception:
        db.rollback()
        log.exception("Error al actualizar estado de envio")
    finally:
        db.close()


# --------------------------------------------------------------------------
# Rutas
# --------------------------------------------------------------------------

@router.get("/webhook")
def verificar_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    esperado = _env("BOT_WA_VERIFY_TOKEN")
    if hub_mode == "subscribe" and esperado and hub_verify_token == esperado:
        return PlainTextResponse(hub_challenge or "")
    raise HTTPException(status_code=403, detail="Token de verificación incorrecto")


@router.post("/webhook")
async def recibir_webhook(request: Request, background: BackgroundTasks):
    cuerpo = await request.body()
    if not _firma_valida(cuerpo, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=403, detail="Firma inválida")
    try:
        data = json.loads(cuerpo or b"{}")
    except ValueError:
        return {"ok": True}

    phone_id_bot = _env("BOT_WA_PHONE_NUMBER_ID")
    for entry in data.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            if (value.get("metadata") or {}).get("phone_number_id") != phone_id_bot:
                continue  # otro numero (p. ej. reportes): no es del bot
            nombres = {
                c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in value.get("contacts") or []
            }
            for msg in value.get("messages") or []:
                wa_id = msg.get("from")
                if wa_id:
                    background.add_task(procesar_entrante, wa_id, nombres.get(wa_id), msg)
            for st in value.get("statuses") or []:
                background.add_task(actualizar_estado, st)

    # Meta necesita un 200 rapido; el trabajo sigue en segundo plano.
    return {"ok": True}
