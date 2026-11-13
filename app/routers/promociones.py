from collections import Counter
from datetime import date, datetime
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db
from app.routers.usuarios import get_current_user

router = APIRouter()

ZONA = ZoneInfo("America/Mexico_City")
MODULO_CADENAS = 7
LIMITE_PIEZA_CARA = 1000  # piezas de mas de $1,000 tienen tope y nunca llevan descuento


def hoy_mx() -> date:
    return datetime.now(ZONA).date()


def _query_vigentes(db: Session, hoy: date):
    P = models.Promocion
    return db.query(P).filter(
        P.activa == True,  # noqa: E712
        or_(P.fecha_inicio.is_(None), P.fecha_inicio <= hoy),
        or_(P.fecha_fin.is_(None), P.fecha_fin >= hoy),
    )


def promo_vigente(db: Session, codigo: str, hoy: Optional[date] = None) -> Optional[models.Promocion]:
    hoy = hoy or hoy_mx()
    return (
        _query_vigentes(db, hoy)
        .filter(func.upper(func.trim(models.Promocion.codigo)) == codigo.strip().upper())
        .first()
    )


def _modulo_id(usuario: models.Usuario) -> Optional[int]:
    return usuario.modulo.id if usuario.modulo else usuario.modulo_id


def aplica_en_modulo(promo: models.Promocion, modulo_id: Optional[int]) -> bool:
    """solo_modulos null = todos los módulos; si trae lista, solo esos."""
    if promo.solo_modulos is None:
        return True
    return modulo_id is not None and modulo_id in promo.solo_modulos


@router.get("/activas")
def promociones_activas(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    modulo_id = _modulo_id(current_user)
    if modulo_id == MODULO_CADENAS:
        return []
    promos = _query_vigentes(db, hoy_mx()).order_by(models.Promocion.id).all()
    return [
        {"codigo": p.codigo, "nombre": p.nombre, "claves": p.claves or []}
        for p in promos
        if aplica_en_modulo(p, modulo_id)
    ]


# ── Calculo de precios ────────────────────────────────────────────────────────

def calcular_precios_promo(codigo: str, renglones: List[dict], claves_promo: Optional[List[str]] = None) -> List[Tuple[int, int, float]]:
    """Funcion pura. renglones: [{idx, cantidad, precio_original, clave}].
    Regresa [(idx, cantidad, precio_unitario)]: un renglon por cada combinacion
    producto/precio, asi una pieza con descuento queda separada de las demas.
    Lanza ValueError con el mensaje para el usuario si la venta no cumple."""
    codigo = codigo.strip().upper()

    if codigo == "FUNDAS30":
        permitidas = {c.strip().upper() for c in (claves_promo or []) if c}
        salida = []
        for r in renglones:
            if r["clave"] not in permitidas:
                raise ValueError(f"La clave {r['clave']} no participa en la promoción FUNDAS30.")
            salida.append((r["idx"], r["cantidad"], round(r["precio_original"] * 0.70, 2)))
        return salida

    if codigo == "3X2":
        grupo, factor = 3, 0.0
    elif codigo == "2DA30":
        grupo, factor = 2, 0.70
    else:
        raise ValueError(f"La promoción {codigo} no está soportada.")

    piezas = [(float(r["precio_original"]), r["idx"]) for r in renglones for _ in range(r["cantidad"])]
    n = len(piezas)
    if n == 0 or n % grupo != 0:
        raise ValueError(f"La promoción {codigo} requiere un número de piezas múltiplo de {grupo} (llevas {n}).")

    bloques = n // grupo
    caras = sum(1 for p, _ in piezas if p > LIMITE_PIEZA_CARA)
    if caras > bloques:
        raise ValueError(
            f"En {codigo} solo se permite 1 pieza de más de $1,000 por cada {grupo} piezas "
            f"(llevas {caras} en {n} piezas)."
        )

    piezas.sort(key=lambda x: (x[0], x[1]))
    con_descuento = piezas[:bloques]
    if any(p > LIMITE_PIEZA_CARA for p, _ in con_descuento):
        raise ValueError(f"En {codigo} una pieza de más de $1,000 no puede llevar el descuento.")

    conteo: Counter = Counter()
    for i, (p, idx) in enumerate(piezas):
        precio = round(p * factor, 2) if i < bloques else round(p, 2)
        conteo[(idx, precio)] += 1
    return sorted(
        ((idx, c, precio) for (idx, precio), c in conteo.items()),
        key=lambda x: (x[0], -x[2]),
    )


def preparar_renglones_promocion(
    db: Session, venta: schemas.VentaMultipleCreate, current_user: models.Usuario
) -> List[Tuple[schemas.ProductoEnVenta, float, str]]:
    """Valida la venta con promocion y regresa [(producto, precio_original, codigo)]
    ya con precio_unitario y cantidad recalculados por el backend."""
    def rechazar(msg: str):
        raise HTTPException(status_code=400, detail=msg)

    if (
        (venta.metodo_pago or "").strip().lower() == "dividido"
        or venta.folio
        or any(p.skip_comision for p in venta.productos)
    ):
        rechazar("Las promociones no aplican con pago dividido")

    if _modulo_id(current_user) == MODULO_CADENAS:
        rechazar("Las promociones no aplican en Cadenas.")

    # Carrito mixto permitido: los productos sin promocion pasan a precio normal.
    con_promo = [(idx, p) for idx, p in enumerate(venta.productos) if (p.promocion or "").strip()]
    codigos = {p.promocion.strip().upper() for _, p in con_promo}
    # FUNDAS30 se calcula aparte (30% sobre sus propias piezas) y puede convivir
    # con 3X2 o 2DA30 en el mismo ticket. Lo que sigue prohibido es mezclar 3X2
    # con 2DA30 en la misma venta.
    codigos_grupo = {c for c in codigos if c != "FUNDAS30"}
    if len(codigos_grupo) > 1:
        rechazar("No se pueden combinar dos promociones distintas en la misma venta.")

    promos_por_codigo: dict = {}
    for cod in codigos:
        promo = promo_vigente(db, cod)
        if not promo:
            rechazar(f"La promoción {cod} no existe o no está activa hoy.")
        if not aplica_en_modulo(promo, _modulo_id(current_user)):
            rechazar("La promoción no aplica en este módulo.")
        promos_por_codigo[cod] = promo

    renglones_por_codigo: dict = {}
    originales: dict = {}
    for idx, item in con_promo:
        cod = item.promocion.strip().upper()
        if (item.tipo_producto or "").strip().lower() == "telefono" or "TELEFONO" in item.producto.strip().upper():
            rechazar(f"La promoción solo aplica a accesorios ({item.producto} no lo es).")
        if item.cantidad is None or item.cantidad <= 0:
            rechazar(f"Cantidad inválida para {item.producto}.")
        clave = (item.clave or "").strip().upper()
        if not clave:
            rechazar(f"El producto {item.producto} no trae clave; no se puede aplicar la promoción.")
        inv = (
            db.query(models.InventarioGeneral)
            .filter(func.upper(func.trim(models.InventarioGeneral.clave)) == clave)
            .first()
        )
        if not inv or inv.precio is None:
            rechazar(f"No se encontró el precio de la clave {clave} en el inventario general.")
        precio_original = float(inv.precio)
        renglones_por_codigo.setdefault(cod, []).append(
            {"idx": idx, "cantidad": item.cantidad, "precio_original": precio_original, "clave": clave}
        )
        originales[idx] = precio_original

    por_idx: dict = {}
    for cod, renglones in renglones_por_codigo.items():
        promo = promos_por_codigo[cod]
        try:
            calculados = calcular_precios_promo(cod, renglones, promo.claves)
        except ValueError as e:
            rechazar(str(e))
        for idx, cantidad, precio in calculados:
            por_idx.setdefault(idx, []).append((
                venta.productos[idx].model_copy(update={"cantidad": cantidad, "precio_unitario": precio}),
                originales[idx],
                promo.codigo,
            ))

    # Respeta el orden del carrito; los productos sin promocion van tal cual.
    salida = []
    for idx, item in enumerate(venta.productos):
        salida.extend(por_idx.get(idx, [(item, None, None)]))
    return salida
