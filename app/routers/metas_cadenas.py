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
# Periodo = mes calendario; nada fuera del mes cuenta. Las semanas son
# bloques fijos dentro del mes: 1 = dias 1-7, 2 = 8-14, 3 = 15-21,
# 4 = 22-28. Del 29 en adelante no hay semana: esos dias solo cuentan para
# la meta diaria y la mensual.
#
# Se aplica la fila de metas_tienda vigente hoy (mayor vigencia_inicio <= hoy)
# a todos los dias y semanas del mes.


def _hoy() -> date:
    return datetime.now(ZONA).replace(tzinfo=None).date()


def _fin_de_mes(d: date) -> date:
    siguiente = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return siguiente - timedelta(days=1)


SEMANAS_POR_MES = 4
DIAS_POR_SEMANA = 7


def _semanas_del_mes(inicio_mes: date) -> list:
    # Bloques fijos: (numero, primer dia, ultimo dia). Siempre caben en el mes
    # porque el mes mas corto tiene 28 dias.
    return [
        (
            n,
            inicio_mes + timedelta(days=(n - 1) * DIAS_POR_SEMANA),
            inicio_mes + timedelta(days=n * DIAS_POR_SEMANA - 1),
        )
        for n in range(1, SEMANAS_POR_MES + 1)
    ]


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
    semanas_mes = _semanas_del_mes(inicio_mes)
    # Bloque en curso; None del dia 29 en adelante.
    semana_actual = next((s for s in semanas_mes if s[1] <= hoy <= s[2]), None)

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
    """), {"tienda_id": tienda_id, "desde": inicio_mes, "hasta": fin_mes}).mappings().all()

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

    # --- Tienda: dia, semana (bloque en curso) y mes ---
    dia_sub, dia_val = _sumar(conteos, hoy, hoy)
    mes_sub, mes_val = _sumar(conteos, inicio_mes, fin_mes)

    semana = None
    if semana_actual is not None:
        numero, inicio_semana, fin_semana = semana_actual
        sem_sub, sem_val = _sumar(conteos, inicio_semana, fin_semana)
        dias_semana = []
        for i in range(DIAS_POR_SEMANA):
            d = inicio_semana + timedelta(days=i)
            s, v = _sumar(conteos, d, d)
            dias_semana.append({
                "fecha": d.isoformat(),
                "dia": DIAS[d.weekday()],
                "num_dia": d.day,
                "subidas": s,
                "validadas": v,
                "cumplida": _cumple(v, meta["meta_diaria"]),
            })
        semana = {
            "numero": numero,
            "inicio": inicio_semana.isoformat(),
            "fin": fin_semana.isoformat(),
            "subidas": sem_sub,
            "validadas": sem_val,
            "meta": meta["meta_semanal"],
            "cumplida": _cumple(sem_val, meta["meta_semanal"]),
            "dias": dias_semana,
        }
        sem_val_tienda = sem_val
    else:
        sem_val_tienda = 0

    # --- Promotores ---
    promotores = []
    for u in usuarios:
        if not u["activo"] and u["id"] not in con_actividad:
            continue
        uid = u["id"]
        p_dia = _sumar(conteos, hoy, hoy, uid)
        p_sem = (
            _sumar(conteos, semana_actual[1], semana_actual[2], uid)
            if semana_actual is not None else (0, 0)
        )
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
            "participacion_semana_pct": _pct(p_sem[1], sem_val_tienda),
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

    # Las 4 semanas siempre salen. Solo las cerradas suman: la en curso no
    # cuenta hasta que termina su dia 7, y las pendientes aun no empiezan.
    semanas = []
    semanas_ganadas = 0
    semana_en_curso_califica = False
    for numero, inicio_bloque, fin_bloque in semanas_mes:
        if fin_bloque < hoy:
            estado = "cerrada"
        elif inicio_bloque <= hoy:
            estado = "en_curso"
        else:
            estado = "pendiente"
        _s, v_tienda = _sumar(conteos, inicio_bloque, fin_bloque)
        _s, v_mio = _sumar(conteos, inicio_bloque, fin_bloque, yo)
        cumplida = _cumple(v_tienda, meta["meta_semanal"])
        califica = cumplida and v_tienda > 0 and v_mio * 100 >= PARTICIPACION_SEMANAL_MIN * v_tienda
        ganada = califica and estado == "cerrada"
        if ganada:
            semanas_ganadas += 1
        if estado == "en_curso":
            semana_en_curso_califica = califica
        semanas.append({
            "numero": numero,
            "inicio": inicio_bloque.isoformat(),
            "fin": fin_bloque.isoformat(),
            "estado": estado,
            "validadas_tienda": v_tienda,
            "mis_validadas": v_mio,
            "participacion_pct": _pct(v_mio, v_tienda),
            "cumplida": cumplida,
            "califica": califica,
            "ganada": ganada,
        })

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
        "semana": semana,
        "sin_semana": semana is None,
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
