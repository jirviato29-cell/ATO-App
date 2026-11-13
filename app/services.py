
from datetime import date, timedelta
from sqlalchemy.orm import Session
from app import models
from sqlalchemy import case, func

from app.congelamiento import aplicar_congelamiento, dias_congelados_batch


# Telefono vendido abajo del precio (ventas.py lo marca con comision_id=None y
# comision_monto=0): no lleva los $10 de Contado. Con comision_monto NULL (el
# caso normal y todo el historico) los $10 se siguen sumando como siempre.
_ABAJO_DEL_PRECIO = (models.Venta.comision_monto == 0) & models.Venta.comision_id.is_(None)


def _abajo_del_precio(v) -> bool:
    return v.comision_monto is not None and v.comision_monto == 0 and v.comision_id is None



from sqlalchemy import func, case


def _acumulador_congelamiento() -> dict:
    return {"total": 0.0, "fechas_congeladas": set()}


def _resumen_congelamiento(fechas: set) -> dict:
    return {
        "dias_congelados": len(fechas),
        "congelado": bool(fechas),
        "fechas_congeladas": sorted(fechas),
    }


def _comisiones_detalle(db: Session, inicio: date, fin: date) -> dict:
    """{empleado_id: {total, dias_congelados, congelado, fechas_congeladas}}.

    Misma formula de comision de siempre. El unico cambio es que el agregado
    ahora agrupa tambien por fecha, para poder marcar el dia congelado sin
    tocar el calculo. dias_congelados_batch se llama UNA vez por request.
    """
    _base = func.coalesce(models.Comision.cantidad, 0)
    _extra = case(
        (_ABAJO_DEL_PRECIO, 0),
        (models.Venta.tipo_venta == "Contado",  10),
        (models.Venta.tipo_venta == "Pajoy",   100),
        (models.Venta.tipo_venta == "Paguitos",110),
        else_=0
    )
    # Contado con comision especial: solo base (sin sumar $10)
    _unitaria_tel = case(
        (
            (models.Venta.tipo_venta == "Contado") & (_base > 0),
            _base
        ),
        else_=_base + _extra
    )
    ventas_rows = (
        db.query(
            models.Venta.empleado_id,
            models.Venta.fecha,
            func.sum(
                case(
                    (models.Venta.tipo_producto == "telefono",
                     _unitaria_tel * models.Venta.cantidad),
                    else_=_base * models.Venta.cantidad
                )
            ).label("total_comisiones")
        )
        .outerjoin(models.Comision, models.Comision.id == models.Venta.comision_id)
        .filter(
            models.Venta.cancelada == False,
            models.Venta.fecha.between(inicio, fin)
        )
        .group_by(models.Venta.empleado_id, models.Venta.fecha)
        .all()
    )

    chips_rows = (
        db.query(
            models.VentaChip.empleado_id,
            models.VentaChip.fecha,
            func.sum(models.VentaChip.comision).label("total_chips")
        )
        .filter(
            models.VentaChip.cancelada == False,
            models.VentaChip.es_incubadora == False,
            models.VentaChip.validado == True,
            models.VentaChip.numero_telefono.isnot(None),
            models.VentaChip.fecha.between(inicio, fin)
        )
        .group_by(models.VentaChip.empleado_id, models.VentaChip.fecha)
        .all()
    )

    empleado_ids = {r.empleado_id for r in ventas_rows}
    empleado_ids |= {r.empleado_id for r in chips_rows}
    empleado_ids.discard(None)
    congelados = dias_congelados_batch(db, sorted(empleado_ids), inicio, fin)

    acumulado: dict = {}

    def _sumar(empleado_id, fecha, monto):
        if empleado_id is None:
            return
        d = acumulado.setdefault(empleado_id, _acumulador_congelamiento())
        pagado, congelado = aplicar_congelamiento(
            float(monto or 0), fecha in congelados.get(empleado_id, set())
        )
        d["total"] += float(pagado or 0)
        if congelado:
            d["fechas_congeladas"].add(fecha)

    for r in ventas_rows:
        _sumar(r.empleado_id, r.fecha, r.total_comisiones)

    for r in chips_rows:
        _sumar(r.empleado_id, r.fecha, r.total_chips)

    return {
        eid: {"total": d["total"], **_resumen_congelamiento(d["fechas_congeladas"])}
        for eid, d in acumulado.items()
    }


def obtener_comisiones_por_empleado_optimizado(db: Session, inicio: date, fin: date):
    """{empleado_id: total_comisiones}. Misma forma de retorno de siempre."""
    return {eid: d["total"] for eid, d in _comisiones_detalle(db, inicio, fin).items()}


def obtener_congelamiento_por_empleado(db: Session, inicio: date, fin: date) -> dict:
    """{empleado_id: {dias_congelados, congelado, fechas_congeladas}}.

    Companion de obtener_comisiones_por_empleado_optimizado, que conserva su
    forma de retorno plana para no romper a quien ya la consume.
    """
    return {
        eid: {k: v for k, v in d.items() if k != "total"}
        for eid, d in _comisiones_detalle(db, inicio, fin).items()
    }


def obtener_desglose_comisiones_por_empleado(db: Session, inicio: date, fin: date) -> dict:
    """Returns {empleado_id: {accesorios, telefonos, chips, total}} in one query per table.

    Agrega ademas dias_congelados / congelado / fechas_congeladas por empleado.
    El agregado agrupa por (empleado_id, fecha) para poder marcar el dia; las
    formulas de comision son identicas a las de antes.
    """
    ventas_rows = (
        db.query(
            models.Venta.empleado_id,
            models.Venta.fecha,
            func.sum(
                case(
                    (models.Venta.tipo_producto != "telefono",
                     func.coalesce(models.Comision.cantidad, 0) * models.Venta.cantidad),
                    else_=0
                )
            ).label("total_accesorios"),
            func.sum(
                case(
                    (models.Venta.tipo_producto == "telefono",
                     case(
                         (
                             (models.Venta.tipo_venta == "Contado") &
                             (func.coalesce(models.Comision.cantidad, 0) > 0),
                             func.coalesce(models.Comision.cantidad, 0) * models.Venta.cantidad
                         ),
                         else_=(
                             func.coalesce(models.Comision.cantidad, 0) +
                             case(
                                 (_ABAJO_DEL_PRECIO, 0),
                                 (models.Venta.tipo_venta == "Contado",  10),
                                 (models.Venta.tipo_venta == "Pajoy",   100),
                                 (models.Venta.tipo_venta == "Paguitos",110),
                                 else_=0
                             )
                         ) * models.Venta.cantidad
                     )),
                    else_=0
                )
            ).label("total_telefonos"),
        )
        .outerjoin(models.Comision, models.Comision.id == models.Venta.comision_id)
        .filter(models.Venta.cancelada == False, models.Venta.fecha.between(inicio, fin))
        .group_by(models.Venta.empleado_id, models.Venta.fecha)
        .all()
    )

    chips_rows = (
        db.query(
            models.VentaChip.empleado_id,
            models.VentaChip.fecha,
            func.sum(models.VentaChip.comision).label("total_chips")
        )
        .filter(
            models.VentaChip.cancelada == False,
            models.VentaChip.es_incubadora == False,
            models.VentaChip.validado == True,
            models.VentaChip.numero_telefono.isnot(None),
            models.VentaChip.fecha.between(inicio, fin)
        )
        .group_by(models.VentaChip.empleado_id, models.VentaChip.fecha)
        .all()
    )

    empleado_ids = {r.empleado_id for r in ventas_rows}
    empleado_ids |= {r.empleado_id for r in chips_rows}
    empleado_ids.discard(None)
    congelados = dias_congelados_batch(db, sorted(empleado_ids), inicio, fin)

    def _nuevo() -> dict:
        return {
            "accesorios": 0.0,
            "telefonos": 0.0,
            "chips": 0.0,
            "total": 0.0,
            "fechas_congeladas": set(),
        }

    result: dict = {}

    for r in ventas_rows:
        if r.empleado_id is None:
            continue
        d = result.setdefault(r.empleado_id, _nuevo())
        esta_congelado = r.fecha in congelados.get(r.empleado_id, set())
        acc, marcado = aplicar_congelamiento(float(r.total_accesorios or 0), esta_congelado)
        tel, _ = aplicar_congelamiento(float(r.total_telefonos or 0), esta_congelado)
        d["accesorios"] += acc
        d["telefonos"] += tel
        d["total"] += acc + tel
        if marcado:
            d["fechas_congeladas"].add(r.fecha)

    for r in chips_rows:
        if r.empleado_id is None:
            continue
        d = result.setdefault(r.empleado_id, _nuevo())
        esta_congelado = r.fecha in congelados.get(r.empleado_id, set())
        chips, marcado = aplicar_congelamiento(float(r.total_chips or 0), esta_congelado)
        d["chips"] += chips
        d["total"] += chips
        if marcado:
            d["fechas_congeladas"].add(r.fecha)

    for eid, d in result.items():
        fechas = d.pop("fechas_congeladas")
        d.update(_resumen_congelamiento(fechas))

    return result


def calcular_totales_comisiones(
    db: Session,
    empleado_id: int,
    inicio: date,
    fin: date
) -> dict:

    ventas = db.query(models.Venta).filter(
        models.Venta.empleado_id == empleado_id,
        models.Venta.fecha >= inicio,
        models.Venta.fecha <= fin,
        models.Venta.cancelada == False
    ).all()

    ventas_chips = db.query(models.VentaChip).filter(
        models.VentaChip.empleado_id == empleado_id,
        models.VentaChip.cancelada == False,
        models.VentaChip.es_incubadora == False,
        models.VentaChip.validado == True,
        models.VentaChip.numero_telefono.isnot(None),
        models.VentaChip.fecha >= inicio,
        models.VentaChip.fecha <= fin,
    ).all()

    # Una sola llamada por request, fuera de los bucles de abajo.
    congelados = dias_congelados_batch(db, [empleado_id], inicio, fin).get(empleado_id, set())
    fechas_congeladas: set = set()

    total_accesorios = 0.0
    total_telefonos = 0.0
    total_chips = 0.0

    _extra_por_tipo = {"Contado": 10, "Paguitos": 110, "Pajoy": 100}

    for v in ventas:
        comision_base = getattr(getattr(v, "comision_obj", None), "cantidad", 0) or 0
        cantidad = getattr(v, "cantidad", 0) or 0

        if v.tipo_producto == "telefono":
            tipo = v.tipo_venta or ""
            if tipo == "Contado" and comision_base > 0:
                comision_total = comision_base * cantidad
            elif _abajo_del_precio(v):
                comision_total = 0.0
            else:
                comision_total = (comision_base + _extra_por_tipo.get(tipo, 0)) * cantidad
            comision_total, marcado = aplicar_congelamiento(
                comision_total, v.fecha in congelados
            )
            if marcado:
                fechas_congeladas.add(v.fecha)
            total_telefonos += comision_total

        elif v.tipo_producto == "accesorios":
            comision_total, marcado = aplicar_congelamiento(
                comision_base * cantidad, v.fecha in congelados
            )
            if marcado:
                fechas_congeladas.add(v.fecha)
            total_accesorios += comision_total

    for v in ventas_chips:
        comision_chip, marcado = aplicar_congelamiento(
            float(getattr(v, "comision", 0) or 0), v.fecha in congelados
        )
        if marcado:
            fechas_congeladas.add(v.fecha)
        total_chips += comision_chip

    return {
        "accesorios": total_accesorios,
        "telefonos": total_telefonos,
        "chips": total_chips,
        "total": total_accesorios + total_telefonos + total_chips,
        **_resumen_congelamiento(fechas_congeladas),
    }
