
import calendar
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy import extract, func, text
from sqlalchemy.orm import Session

from app import models
from app.config import get_current_user
from app.database import get_db
from app.routers.direccion import MODULOS_EXCLUIR_SQL  # fuente única — mantener sincronizado

ZONA = ZoneInfo("America/Mexico_City")
# IMPORTANTE: si cambias este valor, actualiza también el de direccion.py
FACTOR_CRECIMIENTO = 1.03

router = APIRouter()


@router.get("/ranking-modulos")
def ranking_modulos(
    db: Session = Depends(get_db),
    _: models.Usuario = Depends(get_current_user),
):
    ahora = datetime.now(ZONA)
    hoy = ahora.date()
    primer_dia = hoy.replace(day=1)
    _, ultimo_dia = calendar.monthrange(hoy.year, hoy.month)
    dias_transcurridos = hoy.day

    # ── Ventas del mes (accesorios + teléfonos) ───────────────────────────────
    venta_rows = (
        db.query(
            models.Modulo.nombre.label("modulo"),
            models.Venta.tipo_producto,
            models.Venta.tipo_venta,
            func.count(models.Venta.id).label("cnt"),
            func.sum(models.Venta.precio_unitario * models.Venta.cantidad).label("monto"),
        )
        .select_from(models.Venta)
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(
            models.Venta.fecha >= primer_dia,
            models.Venta.fecha <= hoy,
            models.Venta.cancelada.isnot(True),
            ~models.Modulo.nombre.in_(MODULOS_EXCLUIR_SQL),
        )
        .group_by(models.Modulo.nombre, models.Venta.tipo_producto, models.Venta.tipo_venta)
        .all()
    )

    # ── Chips del mes por módulo ───────────────────────────────────────────────
    chip_rows = (
        db.query(
            models.Modulo.nombre.label("modulo"),
            func.count(models.VentaChip.id).label("cnt"),
        )
        .select_from(models.VentaChip)
        .join(models.Usuario, models.VentaChip.empleado_id == models.Usuario.id)
        .join(models.Modulo, models.Usuario.modulo_id == models.Modulo.id)
        .filter(
            models.VentaChip.fecha >= primer_dia,
            models.VentaChip.fecha <= hoy,
            models.VentaChip.cancelada.isnot(True),
            ~models.Modulo.nombre.in_(MODULOS_EXCLUIR_SQL),
        )
        .group_by(models.Modulo.nombre)
        .all()
    )

    # ── Planes del mes por módulo ─────────────────────────────────────────────
    dt_inicio = datetime(hoy.year, hoy.month, 1, 0, 0, 0)
    dt_fin = datetime(hoy.year, hoy.month, hoy.day, 23, 59, 59, 999999)
    plan_rows = (
        db.query(
            models.Modulo.nombre.label("modulo"),
            func.count(models.Plan.id).label("cnt"),
        )
        .join(models.Modulo, models.Plan.modulo_id == models.Modulo.id)
        .filter(
            models.Plan.fecha >= dt_inicio,
            models.Plan.fecha <= dt_fin,
            ~models.Modulo.nombre.in_(MODULOS_EXCLUIR_SQL),
        )
        .group_by(models.Modulo.nombre)
        .all()
    )

    # ── Historial 12 meses para productividad ─────────────────────────────────
    y_h, m_h = hoy.year, hoy.month
    hist_months: list = []
    for _ in range(12):
        m_h -= 1
        if m_h == 0:
            m_h = 12
            y_h -= 1
        hist_months.append((y_h, m_h))

    hist_oldest = date(hist_months[-1][0], hist_months[-1][1], 1)
    _, hist_newest_ud = calendar.monthrange(hist_months[0][0], hist_months[0][1])
    hist_newest_end = date(hist_months[0][0], hist_months[0][1], hist_newest_ud)
    hist_months_set = set(hist_months)

    hist_rows = (
        db.query(
            models.Modulo.nombre.label("modulo"),
            extract("year", models.Venta.fecha).label("yr"),
            extract("month", models.Venta.fecha).label("mo"),
            func.sum(models.Venta.precio_unitario * models.Venta.cantidad).label("total"),
            func.count(models.Venta.id).label("cnt"),
        )
        .select_from(models.Venta)
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(
            models.Venta.fecha >= hist_oldest,
            models.Venta.fecha <= hist_newest_end,
            models.Venta.cancelada.isnot(True),
            ~models.Modulo.nombre.in_(MODULOS_EXCLUIR_SQL),
        )
        .group_by(
            models.Modulo.nombre,
            extract("year", models.Venta.fecha),
            extract("month", models.Venta.fecha),
        )
        .all()
    )

    hist_map: dict = defaultdict(dict)
    for row in hist_rows:
        key = (int(row.yr), int(row.mo))
        if key in hist_months_set and int(row.cnt or 0) >= 100:
            hist_map[row.modulo][key] = float(row.total or 0)

    # ── Ensamblar datos por módulo ────────────────────────────────────────────
    mod_data: dict = defaultdict(lambda: {
        "total_mxn": 0.0,
        "monto_acc": 0.0,
        "cnt_acc": 0,
        "contado": 0,
        "payjoy": 0,
        "paguitos": 0,
        "total_tels": 0,
        "chips": 0,
        "planes": 0,
    })

    for row in venta_rows:
        tp = (row.tipo_producto or "").strip().lower()
        tv = (row.tipo_venta or "").strip().lower()
        cnt = int(row.cnt or 0)
        monto = float(row.monto or 0)
        mod_data[row.modulo]["total_mxn"] += monto
        if tp == "telefono":
            mod_data[row.modulo]["total_tels"] += cnt
            if tv in ("payjoy", "pajoy"):
                mod_data[row.modulo]["payjoy"] += cnt
            elif tv == "paguitos":
                mod_data[row.modulo]["paguitos"] += cnt
            else:
                mod_data[row.modulo]["contado"] += cnt
        else:
            mod_data[row.modulo]["monto_acc"] += monto
            mod_data[row.modulo]["cnt_acc"] += cnt

    for row in chip_rows:
        mod_data[row.modulo]["chips"] += int(row.cnt or 0)

    for row in plan_rows:
        mod_data[row.modulo]["planes"] += int(row.cnt or 0)

    # ── Calcular productividad y armar ranking ────────────────────────────────
    ranking = []
    for modulo, vals in mod_data.items():
        meses_datos = hist_map.get(modulo, {})
        n = len(meses_datos)
        if n > 0:
            promedio = sum(meses_datos.values()) / n
            meta = (promedio * FACTOR_CRECIMIENTO) * (dias_transcurridos / ultimo_dia)
            productividad = round((vals["total_mxn"] / meta) * 100, 1) if meta > 0 else 0.0
        else:
            productividad = 0.0

        promedio_acc = round(vals["monto_acc"] / dias_transcurridos, 2) if dias_transcurridos > 0 else 0.0

        ranking.append({
            "posicion": 0,
            "codigo_modulo": modulo,
            "promedio_venta_accesorios": promedio_acc,
            "total_accesorios": vals["cnt_acc"],
            "contado": vals["contado"],
            "payjoy": vals["payjoy"],
            "paguitos": vals["paguitos"],
            "tel_payjoy": vals["payjoy"],
            "total_tels": vals["total_tels"],
            "chips": vals["chips"],
            "planes": vals["planes"],
            "productividad": productividad,
        })

    ranking.sort(key=lambda x: x["productividad"], reverse=True)
    for idx, modulo in enumerate(ranking, start=1):
        modulo["posicion"] = idx

    return ranking


@router.get("/ranking-modulos-hoy")
def ranking_modulos_hoy(
    fecha: str | None = None,
    db: Session = Depends(get_db),
    _: models.Usuario = Depends(get_current_user),
):
    ahora = datetime.now(ZONA)
    if fecha:
        try:
            dia = datetime.strptime(fecha, "%Y-%m-%d").date()
        except ValueError:
            dia = ahora.date()
    else:
        dia = ahora.date()

    rows = db.execute(text("""
        SELECT m.nombre AS modulo,
               COALESCE(acc.monto, 0) AS acc_monto,
               COALESCE(tel.cant, 0) AS tel_cantidad,
               COALESCE(pl.cant, 0) AS plan_cantidad,
               COALESCE(accm.monto, 0) AS acc_mes_monto,
               COALESCE(telm.cant, 0) AS tel_mes_cantidad
        FROM modulos m
        LEFT JOIN (
            SELECT modulo_id,
                   SUM(CASE WHEN tipo_producto = 'accesorios'
                            THEN COALESCE(total, precio_unitario * cantidad) ELSE 0 END) AS monto
            FROM ventas
            WHERE fecha = :dia AND (cancelada IS NULL OR cancelada = false) AND (devuelta IS NULL OR devuelta = false)
            GROUP BY modulo_id
        ) acc ON acc.modulo_id = m.id
        LEFT JOIN (
            SELECT modulo_id, SUM(cantidad) AS cant
            FROM ventas
            WHERE fecha = :dia AND tipo_producto = 'telefono'
              AND (cancelada IS NULL OR cancelada = false) AND (devuelta IS NULL OR devuelta = false)
            GROUP BY modulo_id
        ) tel ON tel.modulo_id = m.id
        LEFT JOIN (
            SELECT modulo_id, COUNT(id) AS cant
            FROM planes_tarifarios
            WHERE (fecha AT TIME ZONE 'America/Mexico_City') >= date_trunc('month', (now() AT TIME ZONE 'America/Mexico_City'))
              AND (fecha AT TIME ZONE 'America/Mexico_City') < (date_trunc('month', (now() AT TIME ZONE 'America/Mexico_City')) + INTERVAL '1 month')
            GROUP BY modulo_id
        ) pl ON pl.modulo_id = m.id
        LEFT JOIN (
            SELECT modulo_id,
                   SUM(CASE WHEN tipo_producto = 'accesorios'
                            THEN COALESCE(total, precio_unitario * cantidad) ELSE 0 END) AS monto
            FROM ventas
            WHERE date_trunc('month', fecha) = date_trunc('month', CAST(:dia AS date))
              AND (cancelada IS NULL OR cancelada = false) AND (devuelta IS NULL OR devuelta = false)
            GROUP BY modulo_id
        ) accm ON accm.modulo_id = m.id
        LEFT JOIN (
            SELECT modulo_id, SUM(cantidad) AS cant
            FROM ventas
            WHERE date_trunc('month', fecha) = date_trunc('month', CAST(:dia AS date))
              AND tipo_producto = 'telefono'
              AND (cancelada IS NULL OR cancelada = false) AND (devuelta IS NULL OR devuelta = false)
            GROUP BY modulo_id
        ) telm ON telm.modulo_id = m.id
        WHERE m.activo = true AND m.id NOT IN (7, 21)
    """), {"dia": dia}).mappings().all()

    accesorios = sorted(
        [{"modulo": r["modulo"], "valor": float(r["acc_monto"])} for r in rows],
        key=lambda x: x["valor"], reverse=True
    )
    telefonos = sorted(
        [{"modulo": r["modulo"], "valor": int(r["tel_cantidad"])} for r in rows],
        key=lambda x: x["valor"], reverse=True
    )
    planes = sorted(
        [{"modulo": r["modulo"], "valor": int(r["plan_cantidad"])} for r in rows],
        key=lambda x: x["valor"], reverse=True
    )
    accesorios_mes = sorted(
        [{"modulo": r["modulo"], "valor": float(r["acc_mes_monto"])} for r in rows],
        key=lambda x: x["valor"], reverse=True
    )
    telefonos_mes = sorted(
        [{"modulo": r["modulo"], "valor": int(r["tel_mes_cantidad"])} for r in rows],
        key=lambda x: x["valor"], reverse=True
    )

    return {
        "actualizado": ahora.strftime("%H:%M"),
        "accesorios": accesorios,
        "telefonos": telefonos,
        "planes": planes,
        "accesorios_mes": accesorios_mes,
        "telefonos_mes": telefonos_mes,
    }
