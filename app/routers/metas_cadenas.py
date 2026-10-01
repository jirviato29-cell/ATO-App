from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models
from app.config import get_current_user
from app.database import get_db

router = APIRouter()

ZONA = ZoneInfo("America/Mexico_City")

DIAS = ("Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom")

# Participacion minima del promotor en las validadas de la tienda para cobrar
# la bolsa semanal.
PARTICIPACION_SEMANAL_MIN = 30

# Solo lectura. metas_tienda, usuarios, venta_chips y tiendas viven en la BD;
# aqui no se crean ni se modifican.
#
# Conteo: solo tipo_chip = 'Activacion' y no canceladas. La tienda de cada
# activacion es usuarios.tienda_id del empleado. "subidas" = todas;
# "validadas" = ademas validado = true. Las bolsas siempre usan validadas.
#
# Semana = lunes a domingo; pertenece al mes en que cae su domingo.
# Mes = mes calendario.
#
# Se aplica la fila de metas_tienda vigente hoy (mayor vigencia_inicio <= hoy)
# a todos los dias y semanas del mes.


def _hoy() -> date:
    return datetime.now(ZONA).replace(tzinfo=None).date()


def _lunes_de(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _fin_de_mes(d: date) -> date:
    siguiente = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return siguiente - timedelta(days=1)


def _semanas_del_mes(inicio_mes: date, fin_mes: date) -> list:
    # Una semana por cada domingo del mes: (lunes, domingo).
    domingo = inicio_mes + timedelta(days=(6 - inicio_mes.weekday()))
    semanas = []
    while domingo <= fin_mes:
        semanas.append((domingo - timedelta(days=6), domingo))
        domingo += timedelta(days=7)
    return semanas


def _cumple(valor: int, meta) -> bool:
    return meta is not None and valor >= meta


def _pct(parte: int, total: int) -> float:
    return round(parte * 100 / total, 2) if total else 0.0


def _num(valor) -> float:
    return float(valor) if valor is not None else 0.0


def _sumar(conteos: dict, desde: date, hasta: date, empleado_id=None) -> tuple:
    # conteos: {(empleado_id, fecha): (subidas, validadas)}
    subidas = validadas = 0
    for (emp, fecha), (s, v) in conteos.items():
        if desde <= fecha <= hasta and (empleado_id is None or emp == empleado_id):
            subidas += s
            validadas += v
    return subidas, validadas


@router.get("/mi-avance")
def mi_avance(
    tienda_id: Optional[int] = Query(None),
    current_user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if tienda_id is not None:
        if current_user.rol != models.RolEnum.admin:
            raise HTTPException(status_code=403, detail="Acceso denegado. Se requiere rol: admin")
    else:
        tienda_id = current_user.tienda_id

    if not tienda_id:
        return {"aplica": False}

    hoy = _hoy()

    tienda = db.execute(text("""
        SELECT id, nombre FROM tiendas WHERE id = :tienda_id
    """), {"tienda_id": tienda_id}).mappings().first()
    if tienda is None:
        return {"aplica": False}

    meta = db.execute(text("""
        SELECT meta_diaria, meta_semanal, meta_mensual,
               bolsa_diaria, bolsa_semanal, bolsa_mensual
        FROM metas_tienda
        WHERE tienda_id = :tienda_id
          AND vigencia_inicio <= :hoy
        ORDER BY vigencia_inicio DESC, id DESC
        LIMIT 1
    """), {"tienda_id": tienda_id, "hoy": hoy}).mappings().first()
    if meta is None:
        return {"aplica": False}

    inicio_mes = hoy.replace(day=1)
    fin_mes = _fin_de_mes(hoy)
    inicio_semana = _lunes_de(hoy)
    fin_semana = inicio_semana + timedelta(days=6)
    semanas_mes = [s for s in _semanas_del_mes(inicio_mes, fin_mes) if s[0] <= hoy]

    desde = min([inicio_mes, inicio_semana] + [s[0] for s in semanas_mes])
    hasta = max(fin_mes, fin_semana)

    filas = db.execute(text("""
        SELECT vc.empleado_id,
               vc.fecha,
               COUNT(*) AS subidas,
               COUNT(*) FILTER (WHERE vc.validado IS TRUE) AS validadas
        FROM venta_chips vc
        JOIN usuarios u ON u.id = vc.empleado_id
        WHERE u.tienda_id = :tienda_id
          AND vc.tipo_chip = 'Activacion'
          AND COALESCE(vc.cancelada, false) = false
          AND vc.fecha BETWEEN :desde AND :hasta
        GROUP BY vc.empleado_id, vc.fecha
    """), {"tienda_id": tienda_id, "desde": desde, "hasta": hasta}).mappings().all()

    conteos = {
        (f["empleado_id"], f["fecha"]): (int(f["subidas"]), int(f["validadas"]))
        for f in filas
    }
    con_actividad = {emp for (emp, _f) in conteos}

    usuarios = db.execute(text("""
        SELECT id, nombre_completo, activo
        FROM usuarios
        WHERE tienda_id = :tienda_id
        ORDER BY nombre_completo
    """), {"tienda_id": tienda_id}).mappings().all()

    # --- Tienda: dia, semana y mes ---
    dia_sub, dia_val = _sumar(conteos, hoy, hoy)
    sem_sub, sem_val = _sumar(conteos, inicio_semana, fin_semana)
    mes_sub, mes_val = _sumar(conteos, inicio_mes, fin_mes)

    dias_semana = []
    for i in range(7):
        d = inicio_semana + timedelta(days=i)
        s, v = _sumar(conteos, d, d)
        dias_semana.append({
            "fecha": d.isoformat(),
            "dia": DIAS[i],
            "subidas": s,
            "validadas": v,
            "cumplida": _cumple(v, meta["meta_diaria"]),
        })

    # --- Promotores ---
    promotores = []
    for u in usuarios:
        if not u["activo"] and u["id"] not in con_actividad:
            continue
        uid = u["id"]
        p_dia = _sumar(conteos, hoy, hoy, uid)
        p_sem = _sumar(conteos, inicio_semana, fin_semana, uid)
        p_mes = _sumar(conteos, inicio_mes, fin_mes, uid)
        promotores.append({
            "usuario_id": uid,
            "nombre": u["nombre_completo"],
            "subidas_dia": p_dia[0],
            "validadas_dia": p_dia[1],
            "subidas_semana": p_sem[0],
            "validadas_semana": p_sem[1],
            "subidas_mes": p_mes[0],
            "validadas_mes": p_mes[1],
            "participacion_semana_pct": _pct(p_sem[1], sem_val),
            "participacion_mes_pct": _pct(p_mes[1], mes_val),
        })
    promotores.sort(key=lambda p: (-p["validadas_mes"], p["nombre"] or ""))

    # --- Mi bolsa (siempre con validadas) ---
    yo = current_user.id
    bolsa_diaria = _num(meta["bolsa_diaria"])
    bolsa_semanal = _num(meta["bolsa_semanal"])

    dias_ganados = 0
    d = inicio_mes
    while d <= hoy:
        _s, v_tienda = _sumar(conteos, d, d)
        _s, v_mio = _sumar(conteos, d, d, yo)
        if _cumple(v_tienda, meta["meta_diaria"]) and v_mio >= 1:
            dias_ganados += 1
        d += timedelta(days=1)

    semanas = []
    semanas_ganadas = 0
    for lunes, domingo in semanas_mes:
        _s, v_tienda = _sumar(conteos, lunes, domingo)
        _s, v_mio = _sumar(conteos, lunes, domingo, yo)
        cumplida = _cumple(v_tienda, meta["meta_semanal"])
        participacion = _pct(v_mio, v_tienda)
        califica = cumplida and v_tienda > 0 and v_mio * 100 >= PARTICIPACION_SEMANAL_MIN * v_tienda
        en_curso = domingo >= hoy
        if califica and not en_curso:
            semanas_ganadas += 1
        semanas.append({
            "inicio": lunes.isoformat(),
            "fin": domingo.isoformat(),
            "validadas_tienda": v_tienda,
            "mis_validadas": v_mio,
            "participacion_pct": participacion,
            "cumplida": cumplida,
            "califica": califica,
            "estado": "en_curso" if en_curso else "cerrada",
        })

    # La semana en curso puede pertenecer al mes siguiente (su domingo cae
    # en el otro mes); se evalua igual para el indicador.
    _s, v_mio_sem = _sumar(conteos, inicio_semana, fin_semana, yo)
    semana_en_curso_califica = (
        _cumple(sem_val, meta["meta_semanal"])
        and sem_val > 0
        and v_mio_sem * 100 >= PARTICIPACION_SEMANAL_MIN * sem_val
    )

    _s, v_mio_mes = _sumar(conteos, inicio_mes, fin_mes, yo)
    mensual_estimada = Decimal("0")
    if _cumple(mes_val, meta["meta_mensual"]) and v_mio_mes > 0 and meta["bolsa_mensual"] is not None:
        mensual_estimada = (
            Decimal(str(meta["bolsa_mensual"])) * v_mio_mes / mes_val
        ).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)

    diaria_acumulada = round(bolsa_diaria * dias_ganados, 2)
    semanal_acumulada = round(bolsa_semanal * semanas_ganadas, 2)
    mensual_estimada = float(mensual_estimada)

    return {
        "aplica": True,
        "tienda": {"id": tienda["id"], "nombre": tienda["nombre"]},
        "metas": {
            "diaria": meta["meta_diaria"],
            "semanal": meta["meta_semanal"],
            "mensual": meta["meta_mensual"],
        },
        "montos": {
            "diaria": bolsa_diaria,
            "semanal": bolsa_semanal,
            "mensual": _num(meta["bolsa_mensual"]),
        },
        "dia": {
            "fecha": hoy.isoformat(),
            "subidas": dia_sub,
            "validadas": dia_val,
            "meta": meta["meta_diaria"],
            "cumplida": _cumple(dia_val, meta["meta_diaria"]),
        },
        "semana": {
            "inicio": inicio_semana.isoformat(),
            "fin": fin_semana.isoformat(),
            "subidas": sem_sub,
            "validadas": sem_val,
            "meta": meta["meta_semanal"],
            "cumplida": _cumple(sem_val, meta["meta_semanal"]),
            "dias": dias_semana,
        },
        "mes": {
            "inicio": inicio_mes.isoformat(),
            "fin": fin_mes.isoformat(),
            "subidas": mes_sub,
            "validadas": mes_val,
            "meta": meta["meta_mensual"],
            "cumplida": _cumple(mes_val, meta["meta_mensual"]),
        },
        "promotores": promotores,
        "mi_bolsa": {
            "diaria_acumulada": diaria_acumulada,
            "dias_ganados": dias_ganados,
            "semanal_acumulada": semanal_acumulada,
            "semanas_ganadas": semanas_ganadas,
            "semana_en_curso_califica": semana_en_curso_califica,
            "mensual_estimada": mensual_estimada,
            "total_acumulado": round(diaria_acumulada + semanal_acumulada + mensual_estimada, 2),
            "semanas": semanas,
        },
    }
