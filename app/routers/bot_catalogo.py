"""Catalogo de WhatsApp (449 102 7916) conectado al inventario de ATO.

- GET  /bot-whatsapp/catalogo/feed.csv      -> feed PUBLICO que Meta (Commerce Manager) lee cada dia.
- GET  /bot-whatsapp/catalogo/admin         -> (admin) equipos Telcel con existencia + su ficha del catalogo.
- PUT  /bot-whatsapp/catalogo/admin         -> (admin) guardar titulo / descripcion / activo de un equipo.
- POST /bot-whatsapp/catalogo/admin/foto    -> (admin) subir la foto promo de un equipo al bucket "catalogo".

Tabla bot_catalogo y bucket "catalogo" creados a mano en Supabase (sin create_all).
En el feed solo salen equipos con foto y activos; el precio es el de contado del sistema.
"""

import csv
import io
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.utilidades import verificar_rol_requerido

log = logging.getLogger("uvicorn.error")
router = APIRouter()
solo_admin = verificar_rol_requerido(models.RolEnum.admin)

BUCKET = "catalogo"
PREFIJO = "TELEFONO TELCEL"
WHATSAPP = "524491027916"
LEYENDA = "En plan desde $0 de pago inicial, sujeto a aprobación de crédito."
ENTREGA = "Entrega en cualquiera de nuestros módulos ATO o a domicilio en Aguascalientes, Jesús María y Pabellón."
FOTO_MAX_BYTES = 5 * 1024 * 1024
TIPOS_FOTO = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def _modelo(producto: str) -> str:
    p = (producto or "").strip()
    if p.upper().startswith(PREFIJO):
        p = p[len(PREFIJO):]
    return p.strip()


def _slug(producto: str) -> str:
    s = re.sub(r"[^A-Z0-9]+", "-", _modelo(producto).upper()).strip("-")
    return (s or "EQUIPO")[:90]


def _equipos(db: Session):
    """Telefonos TELCEL con existencia (por IMEI) + los que ya estan en el catalogo, con su precio de contado."""
    return db.execute(text("""
        WITH inv AS (
            SELECT e.producto, COUNT(*) AS piezas
            FROM equipos_telcel e
            WHERE e.estatus IN ('surtido', 'en_bodega')
              AND UPPER(e.producto) LIKE 'TELEFONO TELCEL%'
            GROUP BY e.producto
        ),
        precios AS (
            SELECT e.producto, MAX(g.precio) AS precio
            FROM equipos_telcel e
            JOIN inventario_general g ON UPPER(TRIM(g.clave)) = UPPER(TRIM(e.clave))
            WHERE UPPER(e.producto) LIKE 'TELEFONO TELCEL%'
            GROUP BY e.producto
        ),
        todos AS (
            SELECT producto FROM inv
            UNION
            SELECT producto FROM bot_catalogo
        )
        SELECT t.producto, COALESCE(inv.piezas, 0) AS piezas, p.precio,
               c.titulo, c.descripcion, c.foto_url, COALESCE(c.activo, TRUE) AS activo,
               (c.id IS NOT NULL) AS en_catalogo
        FROM todos t
        LEFT JOIN inv ON inv.producto = t.producto
        LEFT JOIN precios p ON p.producto = t.producto
        LEFT JOIN bot_catalogo c ON c.producto = t.producto
        ORDER BY COALESCE(inv.piezas, 0) DESC, t.producto
    """)).mappings().all()


# --------------------------------------------------------------------------
# Feed publico para Meta
# --------------------------------------------------------------------------

@router.get("/feed.csv")
def feed(db: Session = Depends(get_db)):
    columnas = ["id", "title", "description", "availability", "condition", "price", "link", "image_link", "brand"]
    salida = io.StringIO()
    w = csv.writer(salida)
    w.writerow(columnas)
    for f in _equipos(db):
        if not f["en_catalogo"] or not f["activo"] or not f["foto_url"] or not f["precio"]:
            continue
        modelo = _modelo(f["producto"])
        titulo = (f["titulo"] or modelo).strip()[:150]
        precio = float(f["precio"])
        partes = []
        if f["descripcion"]:
            partes.append(f["descripcion"].strip())
        partes.append(f"Precio de contado ${precio:,.0f}. {LEYENDA}")
        partes.append(ENTREGA)
        link = f"https://wa.me/{WHATSAPP}?text=" + quote(f"Hola, me interesa el {titulo}")
        w.writerow([
            _slug(f["producto"]),
            titulo,
            " ".join(partes)[:5000],
            "in stock" if f["piezas"] > 0 else "out of stock",
            "new",
            f"{precio:.2f} MXN",
            link,
            f["foto_url"],
            (modelo.split() or ["Telcel"])[0].title(),
        ])
    return Response(
        content=salida.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------
# Administracion (solo admin)
# --------------------------------------------------------------------------

@router.get("/admin")
def listar(db: Session = Depends(get_db), _u=Depends(solo_admin)):
    return [
        {
            "producto": f["producto"],
            "modelo": _modelo(f["producto"]),
            "piezas": int(f["piezas"]),
            "precio": float(f["precio"]) if f["precio"] is not None else None,
            "titulo": f["titulo"],
            "descripcion": f["descripcion"],
            "foto_url": f["foto_url"],
            "activo": bool(f["activo"]),
            "en_feed": bool(f["en_catalogo"] and f["activo"] and f["foto_url"] and f["precio"]),
        }
        for f in _equipos(db)
    ]


def _validar_producto(db: Session, producto: str) -> str:
    producto = (producto or "").strip()
    if not producto.upper().startswith(PREFIJO):
        raise HTTPException(400, "Producto no válido")
    existe = db.execute(text("""
        SELECT 1 FROM equipos_telcel WHERE producto = :p
        UNION SELECT 1 FROM bot_catalogo WHERE producto = :p
        LIMIT 1
    """), {"p": producto}).first()
    if not existe:
        raise HTTPException(404, "Ese equipo no está en el inventario")
    return producto


class CambioFicha(BaseModel):
    producto: str
    titulo: Optional[str] = None
    descripcion: Optional[str] = None
    activo: bool = True


@router.put("/admin")
def guardar_ficha(body: CambioFicha, db: Session = Depends(get_db), _u=Depends(solo_admin)):
    producto = _validar_producto(db, body.producto)
    titulo = (body.titulo or "").strip()[:150] or None
    descripcion = (body.descripcion or "").strip()[:2000] or None
    db.execute(text("""
        INSERT INTO bot_catalogo (producto, titulo, descripcion, activo, updated_at)
        VALUES (:p, :t, :d, :a, NOW())
        ON CONFLICT (producto) DO UPDATE
          SET titulo = EXCLUDED.titulo, descripcion = EXCLUDED.descripcion,
              activo = EXCLUDED.activo, updated_at = NOW()
    """), {"p": producto, "t": titulo, "d": descripcion, "a": body.activo})
    db.commit()
    return {"ok": True}


def _subir_a_storage(ruta: str, contenido: bytes, tipo: str) -> str:
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise HTTPException(500, "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY no configurados")
    from supabase import create_client
    try:
        create_client(url, key).storage.from_(BUCKET).upload(
            path=ruta, file=contenido,
            file_options={"content-type": tipo, "upsert": "true"},
        )
    except Exception:
        log.exception("[catalogo] Falló la subida de %s", ruta)
        raise HTTPException(502, "No se pudo subir la foto, intenta de nuevo")
    return f"{url}/storage/v1/object/public/{BUCKET}/{ruta}"


@router.post("/admin/foto")
async def subir_foto(
    producto: str = Form(...),
    foto: UploadFile = File(...),
    db: Session = Depends(get_db),
    _u=Depends(solo_admin),
):
    producto = _validar_producto(db, producto)
    tipo = (foto.content_type or "").lower()
    if tipo not in TIPOS_FOTO:
        raise HTTPException(400, "La foto debe ser JPG, PNG o WEBP")
    contenido = await foto.read()
    if len(contenido) < 2048:
        raise HTTPException(400, "La foto está vacía o dañada")
    if len(contenido) > FOTO_MAX_BYTES:
        raise HTTPException(400, "La foto pesa más de 5 MB")
    # Nombre nuevo en cada subida para que Meta y WhatsApp no se queden con la foto vieja en cache
    ruta = f"{_slug(producto)}-{int(time.time())}.{TIPOS_FOTO[tipo]}"
    foto_url = _subir_a_storage(ruta, contenido, tipo)
    db.execute(text("""
        INSERT INTO bot_catalogo (producto, foto_url, updated_at)
        VALUES (:p, :u, NOW())
        ON CONFLICT (producto) DO UPDATE SET foto_url = EXCLUDED.foto_url, updated_at = NOW()
    """), {"p": producto, "u": foto_url})
    db.commit()
    return {"ok": True, "foto_url": foto_url}
