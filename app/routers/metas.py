from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models
from app.config import get_current_user
from app.database import get_db
from app.utilidades import verificar_rol_requerido

router = APIRouter()

ZONA = ZoneInfo("America/Mexico_City")
ROLES_ADMIN = (models.RolEnum.admin, models.RolEnum.direccion)

# Primer dia con metas vigentes. Los dias anteriores existen en el calendario
# pero no cuentan: el frontend los pinta apagados.
METAS_DESDE = date(2026, 9, 11)

DIAS = ("Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom")

# Solo lectura. Las vistas v_bolsa_* y metas_modulo viven en la BD; aqui no
# se crean ni se modifican.
#
# Acceso: /hoy y /dia para cualquier usuario (venta y nivel del modulo).
# /mi-avance para cualquier usuario con modulo: solo su modulo y su propia fila.
# /mi-semana igual: los 7 dias de la semana en curso, solo sus propios bonos.
# /asesores y /semana solo admin y direccion: exponen lo que cobra cada asesor.
# /config solo admin.
#
# Filtro por rol en las consultas a vistas: (:es_admin OR modulo_id = :modulo_id).
# Admin y direccion ven todos los modulos; cualquier otro rol solo el suyo. Un
# usuario sin modulo_id queda con "modulo_id = NULL", que no devuelve filas.
# En /asesores y /semana se conserva para cuando se amplie el acceso.


def _hoy() -> date:
    return datetime.now(ZONA).date()


def _lunes_de(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _filtro_rol(user: models.Usuario) -> dict:
    return {
        "es_admin": user.rol in ROLES_ADMIN,
        "modulo_id": user.modulo_id,
    }


def _bolsa_dia(db: Session, user: models.Usuario, fecha: date) -> dict:
    # La vista ya trae todos los modulos activos (aun en cero) y la escalera
    # de accesorios completa: nivel_1..nivel_4 y bolsa_1..bolsa_4.
    filas = db.execute(text("""
        SELECT modulo_id, modulo, fecha, venta_dia, nivel, bolsa, avance_pct,
               falta_nivel_1,
               nivel_1, nivel_2, nivel_3, nivel_4,
               bolsa_1, bolsa_2, bolsa_3, bolsa_4
        FROM v_bolsa_diaria
        WHERE fecha = :fecha
          AND (:es_admin OR modulo_id = :modulo_id)
        ORDER BY avance_pct DESC NULLS LAST, modulo
    """), {"fecha": fecha, **_filtro_rol(user)}).mappings().all()

    return {
        "fecha": fecha.isoformat(),
        "modulos": [
            {
                "modulo_id": f["modulo_id"],
                "modulo": f["modulo"],
                "fecha": f["fecha"],
                "venta_dia": f["venta_dia"],
                "nivel": f["nivel"],
                "bolsa": f["bolsa"],
                "avance_pct": f["avance_pct"],
                "falta_nivel_1": f["falta_nivel_1"],
                "escalera": [
                    {"nivel": n, "meta": f[f"nivel_{n}"], "bolsa": f[f"bolsa_{n}"]}
                    for n in range(1, 5)
                ],
            }
            for f in filas
        ],
    }


@router.get("/hoy")
def metas_hoy(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    return _bolsa_dia(db, current_user, _hoy())


@router.get("/dia")
def metas_dia(
    fecha: date | None = Query(None),
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    return _bolsa_dia(db, current_user, fecha or _hoy())


@router.get("/mi-avance")
def metas_mi_avance(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    # Modulo y empleado salen SIEMPRE del usuario autenticado, nunca de la
    # peticion. Sin modulo o sin meta vigente hoy: 204, no es un error.
    if current_user.modulo_id is None:
        return Response(status_code=204)

    hoy = _hoy()
    params = {"fecha": hoy, "modulo_id": current_user.modulo_id}

    modulo = db.execute(text("""
        SELECT modulo, venta_dia, nivel, bolsa,
               nivel_1, nivel_2, nivel_3, nivel_4,
               bolsa_1, bolsa_2, bolsa_3, bolsa_4
        FROM v_bolsa_diaria
        WHERE fecha = :fecha
          AND modulo_id = :modulo_id
    """), params).mappings().first()

    if modulo is None or modulo["nivel_1"] is None:
        return Response(status_code=204)

    escalera = [
        {"nivel": n, "meta": modulo[f"nivel_{n}"], "bolsa": modulo[f"bolsa_{n}"]}
        for n in range(1, 5)
    ]
    venta = modulo["venta_dia"] or 0
    nivel = modulo["nivel"] or 0

    siguiente = next(
        (e for e in escalera if e["nivel"] > nivel and e["meta"] is not None), None
    )
    falta_siguiente = max(siguiente["meta"] - venta, 0) if siguiente else 0

    # Una sola fila de agregados: el numero de participantes del modulo y la
    # fila propia. Nombres y ventas de los companeros no salen de la BD.
    mio = db.execute(text("""
        SELECT COALESCE(MAX(n_participantes), 0) AS n_participantes,
               MAX(venta_asesor) FILTER (WHERE empleado_id = :empleado_id) AS mi_venta,
               MAX(participacion_pct) FILTER (WHERE empleado_id = :empleado_id) AS mi_participacion_pct,
               MAX(le_toca) FILTER (WHERE empleado_id = :empleado_id) AS me_toca
        FROM v_bolsa_asesor
        WHERE fecha = :fecha
          AND modulo_id = :modulo_id
    """), {**params, "empleado_id": current_user.id}).mappings().one()

    return {
        "fecha": hoy.isoformat(),
        "modulo": modulo["modulo"],
        "venta_modulo": venta,
        "nivel": nivel,
        "bolsa": modulo["bolsa"] or 0,
        "falta_siguiente": falta_siguiente,
        "siguiente_nivel": siguiente["nivel"] if siguiente else None,
        "escalera": escalera,
        "mi_venta": mio["mi_venta"] or 0,
        "mi_participacion_pct": mio["mi_participacion_pct"] or 0,
        "n_participantes": mio["n_participantes"],
        "me_toca": mio["me_toca"] or 0,
    }


@router.get("/mi-semana")
def metas_mi_semana(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    # Semana en curso (lunes a domingo) del modulo y el empleado del token.
    # Sin parametros: nunca se consultan los bonos de otro asesor.
    if current_user.modulo_id is None:
        return Response(status_code=204)

    lunes = _lunes_de(_hoy())
    domingo = lunes + timedelta(days=6)

    filas = db.execute(text("""
        SELECT fecha, le_toca, nivel
        FROM v_bolsa_asesor
        WHERE fecha BETWEEN :lunes AND :domingo
          AND modulo_id = :modulo_id
          AND empleado_id = :empleado_id
    """), {
        "lunes": lunes,
        "domingo": domingo,
        "modulo_id": current_user.modulo_id,
        "empleado_id": current_user.id,
    }).mappings().all()

    por_fecha = {f["fecha"]: f for f in filas}

    # Los 7 dias salen siempre, tengan fila o no: el componente los pinta todos.
    dias = []
    for i in range(7):
        fecha = lunes + timedelta(days=i)
        fila = por_fecha.get(fecha)
        dias.append({
            "fecha": fecha.isoformat(),
            "dia": DIAS[i],
            "bono": (fila["le_toca"] if fila else 0) or 0,
            "nivel": (fila["nivel"] if fila else 0) or 0,
            "cuenta": fecha >= METAS_DESDE,
        })

    return {
        "lunes": lunes.isoformat(),
        "domingo": domingo.isoformat(),
        "dias": dias,
        "total": sum(d["bono"] for d in dias),
    }


@router.get("/asesores")
def metas_asesores(
    fecha: date | None = Query(None),
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(
        verificar_rol_requerido([models.RolEnum.admin, models.RolEnum.direccion])
    ),
):
    fecha = fecha or _hoy()

    filas = db.execute(text("""
        SELECT modulo_id, modulo, fecha, empleado_id, asesor, venta_asesor,
               tickets, n_participantes, venta_dia, nivel, bolsa_modulo,
               le_toca, participacion_pct
        FROM v_bolsa_asesor
        WHERE fecha = :fecha
          AND (:es_admin OR modulo_id = :modulo_id)
        ORDER BY modulo, venta_asesor DESC
    """), {"fecha": fecha, **_filtro_rol(current_user)}).mappings().all()

    return {"fecha": fecha.isoformat(), "asesores": [dict(f) for f in filas]}


@router.get("/semana")
def metas_semana(
    lunes: date | None = Query(None),
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(
        verificar_rol_requerido([models.RolEnum.admin, models.RolEnum.direccion])
    ),
):
    # Cualquier dia que llegue se normaliza al lunes de su semana.
    lunes = _lunes_de(lunes or _hoy())

    filas = db.execute(text("""
        SELECT lunes, domingo, paga_el, modulo_id, modulo, empleado_id, asesor,
               dias_trabajados, dias_con_bolsa, dias_record, venta_semana,
               a_pagar
        FROM v_bolsa_semanal
        WHERE lunes = :lunes
          AND (:es_admin OR modulo_id = :modulo_id)
        ORDER BY modulo, a_pagar DESC
    """), {"lunes": lunes, **_filtro_rol(current_user)}).mappings().all()

    return {
        "lunes": lunes.isoformat(),
        "domingo": (lunes + timedelta(days=6)).isoformat(),
        "asesores": [dict(f) for f in filas],
    }


@router.get("/config")
def metas_config(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(verificar_rol_requerido(models.RolEnum.admin)),
):
    hoy = _hoy()

    metas = db.execute(text("""
        SELECT id, modulo_id, vigente_desde, vigente_hasta,
               nivel_1, nivel_2, nivel_3, nivel_4,
               bolsa_1, bolsa_2, bolsa_3, bolsa_4,
               tipo_producto, activo, creado_en
        FROM metas_modulo
        WHERE activo = TRUE
          AND vigente_desde <= :fecha
          AND (vigente_hasta IS NULL OR vigente_hasta >= :fecha)
        ORDER BY modulo_id, tipo_producto
    """), {"fecha": hoy}).mappings().all()

    return {"fecha": hoy.isoformat(), "metas": [dict(m) for m in metas]}
