"""Pantalla "CHATS WHATSAPP" (solo admin): ver las platicas del bot, contestar como
asesor, regresar el chat al bot o cerrarlo, y editar la configuracion del bot.

Prefijo: /bot-whatsapp/admin
Tablas: bot_conversaciones, bot_mensajes, bot_config (creadas a mano en Supabase).
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.routers.bot_whatsapp import GRAPH_URL, _env, enviar_texto, guardar_saliente
from app.utilidades import verificar_rol_requerido

router = APIRouter()
solo_admin = verificar_rol_requerido(models.RolEnum.admin)

VENTANA_HORAS = 24          # Meta solo deja escribir libre dentro de 24 h del ultimo mensaje del cliente
TEXTO_MAXIMO = 4000
ESTADOS = ("bot", "asesor", "cerrado")
CLAVES_EDITABLES = (
    "bot_activo", "ofertas", "preguntas_frecuentes", "reglas_extra",
    "img_libre_400", "img_libre_lineanueva", "img_libre_portabilidad",
)


def _iso(dt) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _ventana_abierta(ultimo_entrante) -> bool:
    if ultimo_entrante is None:
        return False
    if ultimo_entrante.tzinfo is None:
        ultimo_entrante = ultimo_entrante.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - ultimo_entrante < timedelta(hours=VENTANA_HORAS)


# --------------------------------------------------------------------------
# Lista y resumen
# --------------------------------------------------------------------------

@router.get("/resumen")
def resumen(db: Session = Depends(get_db), _u=Depends(solo_admin)):
    fila = db.execute(text("""
        SELECT
          COUNT(*) FILTER (WHERE estado = 'asesor')                   AS con_asesor,
          COUNT(*) FILTER (WHERE estado = 'asesor' AND no_leidos > 0) AS pendientes,
          COALESCE(SUM(no_leidos) FILTER (WHERE estado = 'asesor'), 0) AS no_leidos,
          (SELECT valor FROM bot_config WHERE clave = 'bot_activo')   AS bot_activo
        FROM bot_conversaciones
    """)).mappings().first()
    return {
        "con_asesor": int(fila["con_asesor"] or 0),
        "pendientes": int(fila["pendientes"] or 0),
        "no_leidos": int(fila["no_leidos"] or 0),
        "bot_activo": (fila["bot_activo"] or "").strip().lower() == "true",
    }


@router.get("/conversaciones")
def listar_conversaciones(
    filtro: str = Query("todos"),
    q: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _u=Depends(solo_admin),
):
    condiciones = []
    params: dict = {}
    if filtro in ESTADOS:
        condiciones.append("c.estado = :estado")
        params["estado"] = filtro
    elif filtro == "completos":
        condiciones.append("c.datos_completos")
    if q and q.strip():
        condiciones.append("(c.wa_id LIKE :q OR c.nombre_perfil ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    where = ("WHERE " + " AND ".join(condiciones)) if condiciones else ""
    filas = db.execute(text(f"""
        SELECT c.id, c.wa_id, c.nombre_perfil, c.estado, c.paso, c.datos_completos,
               c.no_leidos, c.datos->>'motivo_asesor' AS motivo, c.atendido_por,
               c.ultimo_mensaje_at,
               u.texto AS ultimo_texto, u.autor AS ultimo_autor,
               (SELECT MAX(created_at) FROM bot_mensajes e
                 WHERE e.conversacion_id = c.id AND e.direccion = 'entrante') AS ultimo_entrante
        FROM bot_conversaciones c
        LEFT JOIN LATERAL (
            SELECT texto, autor FROM bot_mensajes m
            WHERE m.conversacion_id = c.id ORDER BY m.id DESC LIMIT 1
        ) u ON TRUE
        {where}
        ORDER BY (c.estado = 'asesor' AND c.no_leidos > 0) DESC, c.ultimo_mensaje_at DESC
        LIMIT 300
    """), params).mappings().all()
    return [
        {
            "id": f["id"],
            "wa_id": f["wa_id"],
            "nombre": f["nombre_perfil"],
            "estado": f["estado"],
            "paso": f["paso"],
            "datos_completos": f["datos_completos"],
            "no_leidos": f["no_leidos"],
            "motivo": f["motivo"],
            "atendido_por": f["atendido_por"],
            "ultimo_at": _iso(f["ultimo_mensaje_at"]),
            "ultimo_texto": (f["ultimo_texto"] or "")[:160],
            "ultimo_autor": f["ultimo_autor"],
            "ventana_abierta": _ventana_abierta(f["ultimo_entrante"]),
        }
        for f in filas
    ]


# --------------------------------------------------------------------------
# Detalle de una conversacion
# --------------------------------------------------------------------------

def _conversacion(db: Session, conv_id: int):
    c = db.execute(text("""
        SELECT c.*,
               (SELECT MAX(created_at) FROM bot_mensajes e
                 WHERE e.conversacion_id = c.id AND e.direccion = 'entrante') AS ultimo_entrante
        FROM bot_conversaciones c WHERE c.id = :id
    """), {"id": conv_id}).mappings().first()
    if not c:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")
    return c


@router.get("/conversaciones/{conv_id}")
def ver_conversacion(conv_id: int, db: Session = Depends(get_db), _u=Depends(solo_admin)):
    c = _conversacion(db, conv_id)
    mensajes = db.execute(text("""
        SELECT id, direccion, autor, tipo, texto, media_id, estado_envio, created_at
        FROM bot_mensajes WHERE conversacion_id = :id ORDER BY id ASC
    """), {"id": conv_id}).mappings().all()
    db.execute(text("UPDATE bot_conversaciones SET no_leidos = 0 WHERE id = :id"), {"id": conv_id})
    db.commit()
    return {
        "id": c["id"],
        "wa_id": c["wa_id"],
        "nombre": c["nombre_perfil"],
        "estado": c["estado"],
        "paso": c["paso"],
        "datos": c["datos"] or {},
        "datos_completos": c["datos_completos"],
        "atendido_por": c["atendido_por"],
        "ventana_abierta": _ventana_abierta(c["ultimo_entrante"]),
        "mensajes": [
            {
                "id": m["id"],
                "direccion": m["direccion"],
                "autor": m["autor"],
                "tipo": m["tipo"],
                "texto": m["texto"],
                "media_id": m["media_id"],
                "estado_envio": m["estado_envio"],
                "fecha": _iso(m["created_at"]),
            }
            for m in mensajes
        ],
    }


class Respuesta(BaseModel):
    texto: str


@router.post("/conversaciones/{conv_id}/responder")
def responder(
    conv_id: int,
    body: Respuesta,
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(solo_admin),
):
    texto_msg = (body.texto or "").strip()
    if not texto_msg:
        raise HTTPException(status_code=400, detail="Escribe un mensaje")
    if len(texto_msg) > TEXTO_MAXIMO:
        raise HTTPException(status_code=400, detail=f"El mensaje es muy largo (máximo {TEXTO_MAXIMO} caracteres)")
    c = _conversacion(db, conv_id)
    if not _ventana_abierta(c["ultimo_entrante"]):
        raise HTTPException(
            status_code=409,
            detail="Ya pasaron más de 24 horas desde el último mensaje del cliente. "
                   "WhatsApp no deja escribirle hasta que él vuelva a escribir.",
        )
    wid = enviar_texto(c["wa_id"], texto_msg)
    guardar_saliente(db, conv_id, wid, "asesor", "text", texto_msg)
    # Si el asesor contesta, el bot deja de contestar en ese chat
    db.execute(text("""
        UPDATE bot_conversaciones SET
            estado = CASE WHEN estado = 'bot' THEN 'asesor' ELSE estado END,
            atendido_por = :u, no_leidos = 0,
            ultimo_mensaje_at = NOW(), updated_at = NOW()
        WHERE id = :id
    """), {"u": usuario.username, "id": conv_id})
    db.commit()
    if not wid:
        raise HTTPException(status_code=502, detail="WhatsApp no aceptó el mensaje. Revisa el detalle en el chat.")
    return {"ok": True, "wa_message_id": wid}


class CambioEstado(BaseModel):
    estado: str


@router.post("/conversaciones/{conv_id}/estado")
def cambiar_estado(
    conv_id: int,
    body: CambioEstado,
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(solo_admin),
):
    if body.estado not in ESTADOS:
        raise HTTPException(status_code=400, detail="Estado no válido")
    _conversacion(db, conv_id)
    db.execute(text("""
        UPDATE bot_conversaciones SET estado = :e, no_leidos = 0,
            atendido_por = CASE WHEN :e = 'asesor' THEN :u ELSE atendido_por END,
            updated_at = NOW()
        WHERE id = :id
    """), {"e": body.estado, "u": usuario.username, "id": conv_id})
    db.commit()
    return {"ok": True, "estado": body.estado}


# --------------------------------------------------------------------------
# Fotos y documentos que manda el cliente (INE, etc.)
# --------------------------------------------------------------------------

@router.get("/media/{media_id}")
def ver_media(media_id: str, db: Session = Depends(get_db), _u=Depends(solo_admin)):
    # Solo se sirven archivos que de verdad llegaron al bot
    existe = db.execute(text("SELECT 1 FROM bot_mensajes WHERE media_id = :m LIMIT 1"),
                        {"m": media_id}).first()
    if not existe:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    token = _env("BOT_WA_TOKEN")
    if not token:
        raise HTTPException(status_code=503, detail="Falta BOT_WA_TOKEN")
    h = {"Authorization": f"Bearer {token}"}
    try:
        info = requests.get(f"{GRAPH_URL}/{media_id}", headers=h, timeout=20)
        if info.status_code >= 300:
            raise HTTPException(status_code=502, detail="WhatsApp ya no tiene ese archivo (caduca en 30 días)")
        url = info.json().get("url")
        archivo = requests.get(url, headers=h, timeout=30)
        if archivo.status_code >= 300:
            raise HTTPException(status_code=502, detail="No se pudo descargar el archivo")
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="No se pudo descargar el archivo")
    return Response(
        content=archivo.content,
        media_type=archivo.headers.get("Content-Type", "application/octet-stream"),
        headers={"Cache-Control": "private, max-age=3600"},
    )


# --------------------------------------------------------------------------
# Configuracion editable del bot
# --------------------------------------------------------------------------

@router.get("/config")
def leer_config(db: Session = Depends(get_db), _u=Depends(solo_admin)):
    filas = db.execute(text("SELECT clave, valor, updated_at FROM bot_config")).mappings().all()
    datos = {f["clave"]: {"valor": f["valor"], "actualizado": _iso(f["updated_at"])} for f in filas}
    return {clave: datos.get(clave, {"valor": "", "actualizado": None}) for clave in CLAVES_EDITABLES}


class CambioConfig(BaseModel):
    clave: str
    valor: str


@router.put("/config")
def guardar_config(body: CambioConfig, db: Session = Depends(get_db), _u=Depends(solo_admin)):
    if body.clave not in CLAVES_EDITABLES:
        raise HTTPException(status_code=400, detail="Esa configuración no se puede editar aquí")
    valor = body.valor if body.valor is not None else ""
    if body.clave == "bot_activo":
        valor = "true" if valor.strip().lower() == "true" else "false"
    if len(valor) > 20000:
        raise HTTPException(status_code=400, detail="El texto es muy largo")
    db.execute(text("""
        INSERT INTO bot_config (clave, valor, updated_at) VALUES (:c, :v, NOW())
        ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor, updated_at = NOW()
    """), {"c": body.clave, "v": valor})
    db.commit()
    return {"ok": True}
