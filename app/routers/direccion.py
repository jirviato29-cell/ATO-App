import calendar
from collections import defaultdict
from datetime import date, datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import extract, func, or_
from sqlalchemy.orm import Session, joinedload

from app import models, schemas
from app.config import get_current_user
from app.database import get_db

ZONA = ZoneInfo("America/Mexico_City")
MODULOS_EXCLUIR = {"v2", "cadenas c.", "mi2", "bo", "prueba"}
MODULOS_EXCLUIR_SQL = ["V2", "Cadenas C.", "MI2", "BO", "prueba"]
# IMPORTANTE: si cambias este valor, actualiza también el de estadisticas.py
FACTOR_CRECIMIENTO = 1.03

router = APIRouter()


def _verificar_rol(user: models.Usuario):
    if user.is_admin:
        return
    if user.rol not in (models.RolEnum.direccion, models.RolEnum.admin):
        raise HTTPException(status_code=403, detail="Sin permiso para este recurso")


@router.get("/cortes", response_model=Optional[schemas.DireccionCorteResponse])
def obtener_corte_direccion(
    modulo_id: int = Query(...),
    fecha: date = Query(...),
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _verificar_rol(user)

    total_devoluciones = (
        db.query(func.coalesce(func.sum(models.Devolucion.monto), 0.0))
        .filter(
            models.Devolucion.modulo_id == modulo_id,
            models.Devolucion.fecha == fecha
        )
        .scalar()
    ) or 0.0

    corte = (
        db.query(models.CorteDia)
        .filter(
            models.CorteDia.fecha == fecha,
            models.CorteDia.modulo_id == modulo_id,
        )
        .first()
    )

    if corte is None:
        cc = db.query(models.CajaChica).filter(
            models.CajaChica.modulo_id == modulo_id,
            models.CajaChica.fecha == fecha,
        ).first()
        caja_chica_monto = float(cc.monto) if cc else 0.0
        if caja_chica_monto == 0.0 and total_devoluciones == 0.0:
            return None
        return schemas.DireccionCorteResponse(
            id=0,
            fecha=fecha,
            modulo_id=modulo_id,
            accesorios_efectivo=0.0,
            accesorios_tarjeta=0.0,
            accesorios_total=0.0,
            telefonos_efectivo=0.0,
            telefonos_tarjeta=0.0,
            telefonos_total=0.0,
            total_efectivo=0.0,
            total_tarjeta=0.0,
            total_sistema=0.0,
            total_general=0.0,
            adicional_recargas=0.0,
            adicional_transporte=0.0,
            adicional_otros=0.0,
            adicional_mayoreo=0.0,
            adicional_mayoreo_para=None,
            salida_efectivo=0.0,
            nota_salida=None,
            enviado=False,
            revisado_direccion=False,
            revisado_por=None,
            revisado_at=None,
            caja_chica=caja_chica_monto,
            devoluciones=float(total_devoluciones),
            chips_count=0,
            chips_por_tipo={},
            ventas=[],
        )

    chips = (
        db.query(models.VentaChip)
        .join(models.Usuario, models.VentaChip.empleado_id == models.Usuario.id)
        .filter(
            models.Usuario.modulo_id == modulo_id,
            models.VentaChip.fecha == fecha,
            models.VentaChip.cancelada.isnot(True),
        )
        .all()
    )

    chips_por_tipo: dict = {}
    for chip in chips:
        tipo = chip.tipo_chip or "Sin tipo"
        chips_por_tipo[tipo] = chips_por_tipo.get(tipo, 0) + 1

    ventas_db = (
        db.query(models.Venta)
        .options(joinedload(models.Venta.empleado))
        .filter(
            models.Venta.fecha == fecha,
            models.Venta.modulo_id == modulo_id,
            models.Venta.cancelada.isnot(True),
        )
        .all()
    )

    ventas_list = [
        schemas.VentaResumenItem(
            id=v.id,
            producto=v.producto,
            tipo_producto=v.tipo_producto,
            tipo_venta=v.tipo_venta,
            precio_unitario=v.precio_unitario,
            cantidad=v.cantidad,
            total=v.precio_unitario * v.cantidad,
            metodo_pago=v.metodo_pago,
            empleado_username=v.empleado.username if v.empleado else None,
            cancelada=v.cancelada or False,
        )
        for v in ventas_db
    ]

    cc = db.query(models.CajaChica).filter(
        models.CajaChica.modulo_id == modulo_id,
        models.CajaChica.fecha == fecha,
    ).first()
    base_data = schemas.CorteDiaResponse.model_validate(corte).model_dump()
    base_data["caja_chica"] = float(cc.monto) if cc else 0.0
    base_data["devoluciones"] = float(total_devoluciones)
    return schemas.DireccionCorteResponse(
        **base_data,
        chips_count=len(chips),
        chips_por_tipo=chips_por_tipo,
        ventas=ventas_list,
    )


@router.get("/reporte-diario")
def reporte_diario(
    fecha: date = Query(...),
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _verificar_rol(user)

    EXCLUIR_IDS = {21, 7}

    modulos = (
        db.query(models.Modulo)
        .filter(models.Modulo.activo == True)  # noqa: E712
        .order_by(models.Modulo.nombre.asc())
        .all()
    )

    resultado_modulos = []
    res_telefonos_total = 0.0
    res_accesorios_total = 0.0
    res_total_efectivo = 0.0
    res_total_tarjeta = 0.0
    res_total_general = 0.0

    for m in modulos:
        if m.id in EXCLUIR_IDS:
            continue

        corte = obtener_corte_direccion(modulo_id=m.id, fecha=fecha, user=user, db=db)

        telefonos_list: list = []
        accesorios_list: list = []
        tel_total = 0.0
        acc_total = 0.0
        sub_efectivo = 0.0
        sub_tarjeta = 0.0
        sub_total = 0.0
        sin_ventas = False

        if corte is None:
            sin_ventas = True
        else:
            tel_total = corte.telefonos_total or 0.0
            acc_total = corte.accesorios_total or 0.0
            sub_efectivo = corte.total_efectivo or 0.0
            sub_tarjeta = corte.total_tarjeta or 0.0
            sub_total = corte.total_general or 0.0

            ventas_activas = [v for v in corte.ventas if not v.cancelada]

            if tel_total == 0.0 and acc_total == 0.0 and not ventas_activas:
                sin_ventas = True
            else:
                tel_map: dict = {}
                acc_map: dict = {}

                for v in ventas_activas:
                    tp = (v.tipo_producto or "").strip().lower()
                    key = v.producto or ""
                    entry_total = v.total if v.total is not None else (v.precio_unitario * v.cantidad)
                    if tp == "telefono":
                        if key not in tel_map:
                            tel_map[key] = {"cantidad": 0, "total": 0.0}
                        tel_map[key]["cantidad"] += v.cantidad
                        tel_map[key]["total"] += entry_total
                    elif tp == "accesorios":
                        if key not in acc_map:
                            acc_map[key] = {"cantidad": 0, "total": 0.0}
                        acc_map[key]["cantidad"] += v.cantidad
                        acc_map[key]["total"] += entry_total

                for desc in sorted(tel_map):
                    cant = tel_map[desc]["cantidad"]
                    tot = tel_map[desc]["total"]
                    telefonos_list.append({
                        "descripcion": desc,
                        "cantidad": cant,
                        "precio_prom": round(tot / cant, 2) if cant else 0.0,
                        "total": round(tot, 2),
                    })

                for desc in sorted(acc_map):
                    cant = acc_map[desc]["cantidad"]
                    tot = acc_map[desc]["total"]
                    accesorios_list.append({
                        "descripcion": desc,
                        "cantidad": cant,
                        "precio_prom": round(tot / cant, 2) if cant else 0.0,
                        "total": round(tot, 2),
                    })

            res_telefonos_total += tel_total
            res_accesorios_total += acc_total
            res_total_efectivo += sub_efectivo
            res_total_tarjeta += sub_tarjeta
            res_total_general += sub_total

        resultado_modulos.append({
            "modulo_id": m.id,
            "nombre": m.nombre,
            "sin_ventas": sin_ventas,
            "telefonos": telefonos_list,
            "accesorios": accesorios_list,
            "telefonos_total": round(tel_total, 2),
            "accesorios_total": round(acc_total, 2),
            "subtotal_efectivo": round(sub_efectivo, 2),
            "subtotal_tarjeta": round(sub_tarjeta, 2),
            "subtotal_total": round(sub_total, 2),
        })

    return {
        "fecha": str(fecha),
        "resumen": {
            "telefonos_total": round(res_telefonos_total, 2),
            "accesorios_total": round(res_accesorios_total, 2),
            "total_efectivo": round(res_total_efectivo, 2),
            "total_tarjeta": round(res_total_tarjeta, 2),
            "total_general": round(res_total_general, 2),
        },
        "modulos": resultado_modulos,
    }


@router.get("/cortes-pendientes", response_model=List[schemas.CortePendienteItem])
def cortes_pendientes(
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _verificar_rol(user)

    cortes = (
        db.query(models.CorteDia)
        .join(models.Modulo, models.CorteDia.modulo_id == models.Modulo.id)
        .filter(
            models.CorteDia.revisado_direccion == False,  # noqa: E712
            models.CorteDia.fecha >= date(2026, 5, 12),
            models.CorteDia.fecha < datetime.now(ZONA).date(),
        )
        .order_by(models.CorteDia.fecha.desc(), models.Modulo.nombre.asc())
        .all()
    )

    resultado = []
    for c in cortes:
        modulo = c.modulo
        if not modulo or modulo.nombre.lower() in MODULOS_EXCLUIR:
            continue
        ef = (
            (c.accesorios_efectivo or 0)
            + (c.telefonos_efectivo or 0)
            + (c.adicional_recargas or 0)
            + (c.adicional_transporte or 0)
            + (c.adicional_otros or 0)
            + (c.adicional_mayoreo or 0)
            - (c.salida_efectivo or 0)
        )
        ta = (c.accesorios_tarjeta or 0) + (c.telefonos_tarjeta or 0)
        resultado.append(schemas.CortePendienteItem(
            id=c.id,
            modulo_id=c.modulo_id,
            modulo_nombre=modulo.nombre,
            fecha=c.fecha,
            total_efectivo=round(ef, 2),
            total_tarjeta=round(ta, 2),
            total_general=round(ef + ta, 2),
        ))

    return resultado


@router.put("/cortes/{corte_id}/marcar-revisado", response_model=schemas.CorteRevisarResponse)
def marcar_corte_revisado(
    corte_id: int,
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _verificar_rol(user)

    corte = db.query(models.CorteDia).filter(models.CorteDia.id == corte_id).first()
    if not corte:
        raise HTTPException(404, "Corte no encontrado")

    ahora = datetime.now(ZONA)
    corte.revisado_direccion = True
    corte.revisado_por = user.username
    corte.revisado_at = ahora

    db.add(models.NotificacionAsistencia(
        asistencia_id=None,
        usuario_id=user.id,
        username=user.username,
        modulo_id=corte.modulo_id,
        mensaje=f"✅ Tu corte del {corte.fecha} fue revisado por dirección ({user.username})",
        distancia_metros=None,
    ))

    db.commit()
    db.refresh(corte)

    return schemas.CorteRevisarResponse(
        revisado_direccion=corte.revisado_direccion,
        revisado_por=corte.revisado_por,
        revisado_at=corte.revisado_at,
    )


@router.get("/buscar-producto", response_model=List[schemas.ProductoBusquedaResult])
def buscar_producto(
    q: str = Query(default=""),
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _verificar_rol(user)

    if len(q.strip()) < 2:
        return []

    q_norm = q.strip()

    # Modulos donde los telefonos aun NO se controlan por IMEI: siguen contando
    # con inventario_modulo, igual que el resto de productos.
    MODULOS_PENDIENTES = ["M2", "RO", "MF"]

    # 1) Telefonos: contar IMEIs surtidos de equipos_telcel, agrupados por modulo.
    #    Se une a inventario_general por clave (case-insensitive: hay claves en
    #    minuscula) para conocer tipo_producto y usar el nombre canonico.
    telefono_rows = (
        db.query(
            models.InventarioGeneral.producto.label("producto"),
            models.Modulo.nombre.label("modulo_nombre"),
            func.count(func.distinct(models.EquiposTelcel.id)).label("total_cant"),
        )
        .join(models.Modulo, models.EquiposTelcel.modulo_id == models.Modulo.id)
        .join(
            models.InventarioGeneral,
            func.upper(models.InventarioGeneral.clave)
            == func.upper(models.EquiposTelcel.clave),
        )
        .filter(
            models.EquiposTelcel.estatus == "surtido",
            models.InventarioGeneral.tipo_producto == "telefono",
            ~models.Modulo.nombre.in_(MODULOS_EXCLUIR_SQL),
            ~models.Modulo.nombre.in_(MODULOS_PENDIENTES),
            models.InventarioGeneral.producto.ilike(f"%{q_norm}%"),
        )
        .group_by(models.InventarioGeneral.producto, models.Modulo.nombre)
        .all()
    )

    # 2) Resto (accesorios, o telefonos en M2/RO/MF): mismo patron de hoy sobre
    #    inventario_modulo, evitando duplicar los telefonos ya contados por IMEI.
    resto_rows = (
        db.query(
            models.InventarioModulo.producto,
            models.Modulo.nombre.label("modulo_nombre"),
            func.sum(models.InventarioModulo.cantidad).label("total_cant"),
        )
        .join(models.Modulo, models.InventarioModulo.modulo_id == models.Modulo.id)
        .filter(
            models.InventarioModulo.producto.ilike(f"%{q_norm}%"),
            ~models.Modulo.nombre.in_(MODULOS_EXCLUIR_SQL),
            models.InventarioModulo.cantidad > 0,
            or_(
                models.InventarioModulo.tipo_producto != "telefono",
                models.Modulo.nombre.in_(MODULOS_PENDIENTES),
            ),
        )
        .group_by(models.InventarioModulo.producto, models.Modulo.nombre)
        .all()
    )

    # 3) Combinar: sumar cantidades por producto + modulo. Un mismo producto
    #    puede tener varias claves; debe salir una sola fila por modulo (sumada).
    combinado: dict = defaultdict(lambda: defaultdict(int))
    for row in list(telefono_rows) + list(resto_rows):
        combinado[row.producto][row.modulo_nombre] += int(row.total_cant)

    resultado = []
    for producto, modulos_map in combinado.items():
        modulos = [
            schemas.ModuloStockItem(modulo=nombre, cantidad=cant)
            for nombre, cant in modulos_map.items()
        ]
        total = sum(m.cantidad for m in modulos)
        resultado.append(schemas.ProductoBusquedaResult(
            producto=producto,
            total=total,
            modulos=sorted(modulos, key=lambda x: x.cantidad, reverse=True),
        ))

    resultado.sort(key=lambda x: x.total, reverse=True)
    return resultado[:50]


@router.get("/stock-por-modulo", response_model=List[schemas.StockPorModuloItem])
def stock_por_modulo(
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _verificar_rol(user)

    rows = (
        db.query(
            models.Modulo.nombre.label("modulo"),
            func.sum(models.InventarioModulo.cantidad).label("total_productos"),
            func.count(func.distinct(models.InventarioModulo.producto)).label("tipos_distintos"),
        )
        .join(models.InventarioModulo, models.Modulo.id == models.InventarioModulo.modulo_id)
        .filter(~models.Modulo.nombre.in_(MODULOS_EXCLUIR_SQL))
        .group_by(models.Modulo.nombre)
        .order_by(func.sum(models.InventarioModulo.cantidad).desc())
        .all()
    )

    return [
        schemas.StockPorModuloItem(
            modulo=row.modulo,
            total_productos=int(row.total_productos or 0),
            tipos_distintos=int(row.tipos_distintos or 0),
        )
        for row in rows
    ]


MESES_ES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]


@router.get("/estadisticas", response_model=schemas.EstadisticasMesResponse)
def estadisticas_mes(
    mes: Optional[str] = Query(default=None),
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _verificar_rol(user)

    ahora_mx = datetime.now(ZONA)

    if mes:
        try:
            año, m = int(mes[:4]), int(mes[5:7])
        except Exception:
            raise HTTPException(400, "Formato inválido. Use YYYY-MM")
    else:
        año, m = ahora_mx.year, ahora_mx.month

    fecha_inicio = date(año, m, 1)
    _, ultimo_dia = calendar.monthrange(año, m)
    fecha_fin = date(año, m, ultimo_dia)
    dt_inicio = datetime(año, m, 1, 0, 0, 0)
    dt_fin = datetime(año, m, ultimo_dia, 23, 59, 59, 999999)

    # Rango especial para planes: el ultimo dia del mes anterior cuenta para este mes,
    # y el ultimo dia de este mes se pasa al siguiente.
    _mes_ant = 12 if m == 1 else m - 1
    _anio_ant = año - 1 if m == 1 else año
    _, _ult_dia_ant = calendar.monthrange(_anio_ant, _mes_ant)
    planes_inicio = datetime(_anio_ant, _mes_ant, _ult_dia_ant, 0, 0, 0)
    planes_fin = datetime(año, m, ultimo_dia - 1, 23, 59, 59, 999999)

    periodo_texto = f"{MESES_ES[m - 1]} {año}"
    mes_str = f"{año:04d}-{m:02d}"

    # ── Filtros base ──────────────────────────────────────────────────────────
    f_ventas = [
        models.Venta.fecha >= fecha_inicio,
        models.Venta.fecha <= fecha_fin,
        models.Venta.cancelada.isnot(True),
        func.coalesce(models.Venta.devuelta, False).is_(False),
        ~models.Modulo.nombre.in_(MODULOS_EXCLUIR_SQL),
    ]
    f_ventas_tel = f_ventas + [models.Venta.tipo_producto.ilike("telefono")]
    f_ventas_acc = f_ventas + [~models.Venta.tipo_producto.ilike("telefono")]

    f_chips = [
        models.VentaChip.fecha >= fecha_inicio,
        models.VentaChip.fecha <= fecha_fin,
        models.VentaChip.cancelada.isnot(True),
        ~models.Modulo.nombre.in_(MODULOS_EXCLUIR_SQL),
    ]

    # ── Teléfonos (tabla ventas, tipo_producto = 'telefono') ──────────────────
    tel_tipo_rows = (
        db.query(
            models.Venta.tipo_venta,
            func.count(func.distinct(func.coalesce(models.Venta.folio, func.concat('id-', models.Venta.id)))).label("cnt"),
            func.sum(models.Venta.precio_unitario * models.Venta.cantidad).label("monto"),
        )
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(*f_ventas_tel)
        .group_by(models.Venta.tipo_venta)
        .all()
    )

    contado: dict = {"cantidad": 0, "monto": 0.0}
    payjoy: dict = {"cantidad": 0, "monto": 0.0}
    paguitos: dict = {"cantidad": 0, "monto": 0.0}
    plan: dict = {"cantidad": 0, "monto": 0.0}
    sin_clasificar: dict = {"cantidad": 0, "monto": 0.0}

    for row in tel_tipo_rows:
        tv = (row.tipo_venta or "").strip().lower()
        cnt = int(row.cnt or 0)
        monto_t = float(row.monto or 0)
        if tv in ("pajoy", "payjoy"):
            payjoy["cantidad"] += cnt
            payjoy["monto"] += monto_t
        elif tv == "paguitos":
            paguitos["cantidad"] += cnt
            paguitos["monto"] += monto_t
        elif tv == "plan":
            # Pago inicial de un plan tarifario: es su propia categoria, no Contado.
            plan["cantidad"] += cnt
            plan["monto"] += monto_t
        elif not tv:
            sin_clasificar["cantidad"] += cnt
            sin_clasificar["monto"] += monto_t
        else:
            contado["cantidad"] += cnt
            contado["monto"] += monto_t

    total_telefonos = (
        contado["cantidad"] + payjoy["cantidad"] + paguitos["cantidad"]
        + plan["cantidad"] + sin_clasificar["cantidad"]
    )

    # ── Teléfonos por día ─────────────────────────────────────────────────────
    tel_dia_rows = (
        db.query(
            extract("day", models.Venta.fecha).label("dia"),
            func.count(func.distinct(func.coalesce(models.Venta.folio, func.concat('id-', models.Venta.id)))).label("cnt"),
        )
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(*f_ventas_tel)
        .group_by(extract("day", models.Venta.fecha))
        .all()
    )
    _tel_por_dia = {int(r.dia): int(r.cnt or 0) for r in tel_dia_rows}
    telefonos_por_dia = [
        {"dia": d, "cantidad": _tel_por_dia.get(d, 0)}
        for d in range(1, ultimo_dia + 1)
    ]

    # ── Teléfonos top (top 10 modelos, sin prefijo LIBRE/TELCEL) ──────────────
    _modelo_expr = func.trim(
        func.regexp_replace(models.Venta.producto, '^TELEFONO (LIBRE|TELCEL)\\s+', '', 'i')
    )
    tel_top_rows = (
        db.query(
            _modelo_expr.label("modelo"),
            func.sum(models.Venta.cantidad).label("cnt"),
        )
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(*f_ventas_tel)
        .group_by(_modelo_expr)
        .order_by(func.sum(models.Venta.cantidad).desc())
        .limit(10)
        .all()
    )
    telefonos_top = [
        {"modelo": r.modelo, "cantidad": int(r.cnt or 0)}
        for r in tel_top_rows
    ]

    # ── Accesorios ────────────────────────────────────────────────────────────
    acc_agg = (
        db.query(
            func.sum(models.Venta.cantidad).label("total_unidades"),
            func.sum(models.Venta.precio_unitario * models.Venta.cantidad).label("monto_total"),
        )
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(*f_ventas_acc)
        .first()
    )
    total_unidades_acc = int(acc_agg.total_unidades or 0)
    monto_acc = float(acc_agg.monto_total or 0)

    # ── Accesorios por día (promedio $ por módulo activo ese día) ─────────────
    acc_dia_rows = (
        db.query(
            extract("day", models.Venta.fecha).label("dia"),
            func.sum(models.Venta.precio_unitario * models.Venta.cantidad).label("monto"),
            func.count(func.distinct(models.Venta.modulo_id)).label("modulos"),
        )
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(*f_ventas_acc)
        .group_by(extract("day", models.Venta.fecha))
        .all()
    )
    _acc_por_dia = {}
    _acc_total_por_dia = {}
    for r in acc_dia_rows:
        mods = int(r.modulos or 0)
        monto = float(r.monto or 0)
        _acc_por_dia[int(r.dia)] = round(monto / mods, 2) if mods > 0 else 0.0
        _acc_total_por_dia[int(r.dia)] = round(monto, 2)
    accesorios_por_dia = [
        {"dia": d, "promedio": _acc_por_dia.get(d, 0.0), "total": _acc_total_por_dia.get(d, 0.0)}
        for d in range(1, ultimo_dia + 1)
    ]

    top5 = (
        db.query(
            models.Venta.producto,
            func.sum(models.Venta.cantidad).label("total_cantidad"),
            func.sum(models.Venta.precio_unitario * models.Venta.cantidad).label("total_monto"),
        )
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(*f_ventas_acc)
        .group_by(models.Venta.producto)
        .order_by(func.sum(models.Venta.cantidad).desc())
        .limit(5)
        .all()
    )

    top10 = (
        db.query(
            models.Venta.producto,
            func.sum(models.Venta.cantidad).label("total_cantidad"),
            func.sum(models.Venta.precio_unitario * models.Venta.cantidad).label("total_monto"),
        )
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(*f_ventas_acc)
        .group_by(models.Venta.producto)
        .order_by(func.sum(models.Venta.cantidad).desc())
        .limit(10)
        .all()
    )
    accesorios_top_10 = [
        {"producto": r.producto, "cantidad": int(r.total_cantidad or 0), "monto": float(r.total_monto or 0)}
        for r in top10
    ]

    total_mxn_row = (
        db.query(
            func.sum(models.Venta.precio_unitario * models.Venta.cantidad).label("total"),
        )
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(*f_ventas)
        .first()
    )
    total_ventas_mxn = round(float(total_mxn_row.total or 0), 2)

    # ── Chips ─────────────────────────────────────────────────────────────────
    chips_tipo_rows = (
        db.query(
            models.VentaChip.tipo_chip,
            func.count(models.VentaChip.id).label("cnt"),
        )
        .join(models.Usuario, models.VentaChip.empleado_id == models.Usuario.id)
        .join(models.Modulo, models.Usuario.modulo_id == models.Modulo.id)
        .filter(*f_chips)
        .group_by(models.VentaChip.tipo_chip)
        .order_by(func.count(models.VentaChip.id).desc())
        .all()
    )

    chips_monto_rows = (
        db.query(
            models.VentaChip.monto_recarga,
            func.count(models.VentaChip.id).label("cnt"),
        )
        .join(models.Usuario, models.VentaChip.empleado_id == models.Usuario.id)
        .join(models.Modulo, models.Usuario.modulo_id == models.Modulo.id)
        .filter(*f_chips)
        .group_by(models.VentaChip.monto_recarga)
        .all()
    )

    total_chips = sum(int(r.cnt or 0) for r in chips_tipo_rows)

    _BUCKET_ORDER = ["$50", "$100", "$150", "$200", "$Otro"]

    def _bucket_monto(mv: float) -> Optional[str]:
        if mv < 50:
            return None
        elif mv < 90:
            return "$50"
        elif mv < 125:
            return "$100"
        elif mv < 175:
            return "$150"
        elif mv < 225:
            return "$200"
        else:
            return "$Otro"

    monto_buckets: dict = defaultdict(int)
    for row in chips_monto_rows:
        label = _bucket_monto(float(row.monto_recarga or 0))
        if label is not None:
            monto_buckets[label] += int(row.cnt or 0)

    por_monto_recarga = [
        schemas.MontoRecargaStatItem(monto=label, cantidad=cnt)
        for label, cnt in sorted(
            monto_buckets.items(),
            key=lambda x: _BUCKET_ORDER.index(x[0]) if x[0] in _BUCKET_ORDER else 99,
        )
        if cnt > 0
    ]

    # ── Planes ────────────────────────────────────────────────────────────────
    planes_tramite_rows = (
        db.query(
            func.trim(models.PlanTarifario.categoria).label("tramite"),
            func.count(models.PlanTarifario.id).label("cnt"),
        )
        .join(models.Modulo, models.PlanTarifario.modulo_id == models.Modulo.id)
        .filter(
            models.PlanTarifario.fecha >= planes_inicio,
            models.PlanTarifario.fecha <= planes_fin,
            ~models.Modulo.nombre.in_(MODULOS_EXCLUIR_SQL),
        )
        .group_by(func.trim(models.PlanTarifario.categoria))
        .order_by(func.count(models.PlanTarifario.id).desc())
        .all()
    )

    planes_plan_rows = (
        db.query(
            func.trim(models.PlanTarifario.tipo_plan).label("plan"),
            func.count(models.PlanTarifario.id).label("cnt"),
        )
        .join(models.Modulo, models.PlanTarifario.modulo_id == models.Modulo.id)
        .filter(
            models.PlanTarifario.fecha >= planes_inicio,
            models.PlanTarifario.fecha <= planes_fin,
            ~models.Modulo.nombre.in_(MODULOS_EXCLUIR_SQL),
        )
        .group_by(func.trim(models.PlanTarifario.tipo_plan))
        .order_by(func.count(models.PlanTarifario.id).desc())
        .all()
    )

    planes_contratos_rows = (
        db.query(
            func.count(models.PlanTarifario.id).label("cnt"),
            models.PlanTarifario.contrato_listo.label("listo"),
        )
        .join(models.Modulo, models.PlanTarifario.modulo_id == models.Modulo.id)
        .filter(
            models.PlanTarifario.fecha >= planes_inicio,
            models.PlanTarifario.fecha <= planes_fin,
            ~models.Modulo.nombre.in_(MODULOS_EXCLUIR_SQL),
        )
        .group_by(models.PlanTarifario.contrato_listo)
        .all()
    )
    contratos_pendientes = 0
    contratos_listos = 0
    for r in planes_contratos_rows:
        if r.listo is True:
            contratos_listos += int(r.cnt or 0)
        else:
            contratos_pendientes += int(r.cnt or 0)

    total_planes = sum(int(r.cnt or 0) for r in planes_tramite_rows)

    # ── Por módulo ────────────────────────────────────────────────────────────
    modulo_map: dict = defaultdict(lambda: {
        "total_mxn": 0.0,
        "telefonos_contado": 0,
        "telefonos_payjoy": 0,
        "telefonos_paguitos": 0,
        "telefonos_plan": 0,
        "telefonos_total": 0,
        "chips": 0,
        "accesorios": 0,
        "planes": 0,
    })

    acc_mod = (
        db.query(
            models.Modulo.nombre.label("modulo"),
            func.sum(models.Venta.precio_unitario * models.Venta.cantidad).label("monto"),
            func.sum(models.Venta.cantidad).label("unidades"),
        )
        .select_from(models.Venta)
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(*f_ventas_acc)
        .group_by(models.Modulo.nombre)
        .all()
    )
    for row in acc_mod:
        modulo_map[row.modulo]["total_mxn"] += float(row.monto or 0)
        modulo_map[row.modulo]["accesorios"] += int(row.unidades or 0)

    # Teléfonos por módulo+tipo_venta — reutilizado para modulo_map y telefonos_por_modulo
    tel_mod_desglose = (
        db.query(
            models.Modulo.nombre.label("modulo"),
            models.Venta.tipo_venta,
            func.count(func.distinct(func.coalesce(models.Venta.folio, func.concat('id-', models.Venta.id)))).label("cnt"),
            func.sum(models.Venta.precio_unitario * models.Venta.cantidad).label("monto"),
        )
        .select_from(models.Venta)
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(*f_ventas_tel)
        .group_by(models.Modulo.nombre, models.Venta.tipo_venta)
        .all()
    )

    tel_mod_map: dict = defaultdict(
        lambda: {"total": 0, "monto": 0.0, "contado": 0, "payjoy": 0, "paguitos": 0, "plan": 0}
    )
    for row in tel_mod_desglose:
        tv = (row.tipo_venta or "").strip().lower()
        cnt = int(row.cnt or 0)
        mv = float(row.monto or 0)
        modulo_map[row.modulo]["total_mxn"] += mv
        modulo_map[row.modulo]["telefonos_total"] += cnt
        tel_mod_map[row.modulo]["total"] += cnt
        tel_mod_map[row.modulo]["monto"] += mv
        if tv in ("pajoy", "payjoy"):
            modulo_map[row.modulo]["telefonos_payjoy"] += cnt
            tel_mod_map[row.modulo]["payjoy"] += cnt
        elif tv == "paguitos":
            modulo_map[row.modulo]["telefonos_paguitos"] += cnt
            tel_mod_map[row.modulo]["paguitos"] += cnt
        elif tv == "plan":
            # Pago inicial de un plan tarifario: es su propia categoria, no Contado.
            modulo_map[row.modulo]["telefonos_plan"] += cnt
            tel_mod_map[row.modulo]["plan"] += cnt
        else:
            modulo_map[row.modulo]["telefonos_contado"] += cnt
            tel_mod_map[row.modulo]["contado"] += cnt

    chips_mod = (
        db.query(
            models.Modulo.nombre.label("modulo"),
            func.count(models.VentaChip.id).label("cnt"),
        )
        .select_from(models.VentaChip)
        .join(models.Usuario, models.VentaChip.empleado_id == models.Usuario.id)
        .join(models.Modulo, models.Usuario.modulo_id == models.Modulo.id)
        .filter(*f_chips)
        .group_by(models.Modulo.nombre)
        .all()
    )
    for row in chips_mod:
        modulo_map[row.modulo]["chips"] += int(row.cnt or 0)

    planes_mod = (
        db.query(
            models.Modulo.nombre.label("modulo"),
            func.count(models.PlanTarifario.id).label("cnt"),
        )
        .join(models.Modulo, models.PlanTarifario.modulo_id == models.Modulo.id)
        .filter(
            models.PlanTarifario.fecha >= planes_inicio,
            models.PlanTarifario.fecha <= planes_fin,
            ~models.Modulo.nombre.in_(MODULOS_EXCLUIR_SQL),
        )
        .group_by(models.Modulo.nombre)
        .all()
    )
    for row in planes_mod:
        modulo_map[row.modulo]["planes"] += int(row.cnt or 0)

    # ── Productividad: promedio histórico últimos 12 meses ────────────────────
    is_mes_actual = (año == ahora_mx.year and m == ahora_mx.month)
    dias_transcurridos = ahora_mx.day if is_mes_actual else ultimo_dia

    y_h, m_h = año, m
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

    # Solo incluir meses con >= 100 ventas (meses "operativos")
    hist_map: dict = defaultdict(dict)
    for row in hist_rows:
        key = (int(row.yr), int(row.mo))
        if key in hist_months_set and int(row.cnt or 0) >= 100:
            hist_map[row.modulo][key] = float(row.total or 0)

    prod_map: dict = {}
    for mod, vals in modulo_map.items():
        meses_datos = hist_map.get(mod, {})
        n = len(meses_datos)
        if n == 0:
            prod_map[mod] = {"promedio": 0.0, "meta": 0.0, "pct": None, "meses_considerados": 0}
        else:
            total_hist = sum(meses_datos.values())
            promedio = total_hist / n
            meta = (promedio * FACTOR_CRECIMIENTO) * (dias_transcurridos / ultimo_dia)
            pct = round((vals["total_mxn"] / meta) * 100, 1) if meta > 0 else None
            prod_map[mod] = {"promedio": round(promedio, 2), "meta": round(meta, 2), "pct": pct, "meses_considerados": n}

    por_modulo = sorted(
        [
            schemas.ModuloEstadItem(
                modulo=mod,
                total_mxn=round(vals["total_mxn"], 2),
                telefonos_contado=vals["telefonos_contado"],
                telefonos_payjoy=vals["telefonos_payjoy"],
                telefonos_paguitos=vals["telefonos_paguitos"],
                telefonos_plan=vals["telefonos_plan"],
                telefonos_total=vals["telefonos_total"],
                chips=vals["chips"],
                accesorios=vals["accesorios"],
                planes=vals["planes"],
                promedio_historico=prod_map.get(mod, {}).get("promedio", 0.0),
                meta_proporcional=prod_map.get(mod, {}).get("meta", 0.0),
                productividad_pct=prod_map.get(mod, {}).get("pct"),
                meses_considerados=prod_map.get(mod, {}).get("meses_considerados", 0),
            )
            for mod, vals in modulo_map.items()
        ],
        key=lambda x: x.total_mxn,
        reverse=True,
    )

    # ── Ventas por día ────────────────────────────────────────────────────────
    dia_rows = (
        db.query(
            extract("day", models.Venta.fecha).label("dia"),
            func.sum(models.Venta.precio_unitario * models.Venta.cantidad).label("total"),
        )
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(*f_ventas)
        .group_by(extract("day", models.Venta.fecha))
        .all()
    )
    dia_total: dict = {int(r.dia): float(r.total or 0) for r in dia_rows}

    ventas_por_dia = [
        schemas.VentaDiaItem(dia=d, total=round(dia_total.get(d, 0.0), 2))
        for d in range(1, ultimo_dia + 1)
    ]

    # ── Teléfonos por módulo (from ventas table, built from tel_mod_desglose) ─
    telefonos_por_modulo = sorted(
        [
            schemas.TelefonoModuloItem(
                modulo=mod,
                total_telefonos=vals["total"],
                monto_total=round(vals["monto"], 2),
                contado=vals["contado"],
                payjoy=vals["payjoy"],
                paguitos=vals["paguitos"],
                plan=vals["plan"],
            )
            for mod, vals in tel_mod_map.items()
            if vals["total"] > 0
        ],
        key=lambda x: x.total_telefonos,
        reverse=True,
    )

    # ── Respuesta ─────────────────────────────────────────────────────────────
    return schemas.EstadisticasMesResponse(
        mes=mes_str,
        periodo_texto=periodo_texto,
        resumen_general=schemas.ResumenGeneralStats(
            total_ventas_mxn=total_ventas_mxn,
            total_telefonos=total_telefonos,
            total_chips=total_chips,
            total_accesorios=total_unidades_acc,
            total_planes=total_planes,
        ),
        telefonos=schemas.TelefonosStats(
            total=total_telefonos,
            contado=schemas.CantidadMonto(
                cantidad=contado["cantidad"], monto=round(contado["monto"], 2)
            ),
            payjoy=schemas.CantidadMonto(
                cantidad=payjoy["cantidad"], monto=round(payjoy["monto"], 2)
            ),
            paguitos=schemas.CantidadMonto(
                cantidad=paguitos["cantidad"], monto=round(paguitos["monto"], 2)
            ),
            plan=schemas.CantidadMonto(
                cantidad=plan["cantidad"], monto=round(plan["monto"], 2)
            ),
            sin_clasificar=schemas.CantidadMonto(
                cantidad=sin_clasificar["cantidad"], monto=round(sin_clasificar["monto"], 2)
            ),
        ),
        accesorios=schemas.AccesoriosStats(
            total_unidades=total_unidades_acc,
            monto_total=round(monto_acc, 2),
            top_5_productos=[
                schemas.TopProductoItem(
                    producto=r.producto,
                    cantidad=int(r.total_cantidad or 0),
                    monto=round(float(r.total_monto or 0), 2),
                )
                for r in top5
            ],
            top_10_productos=accesorios_top_10,
        ),
        chips=schemas.ChipsStats(
            total=total_chips,
            por_tipo=[
                schemas.TipoChipStatItem(
                    tipo_chip=r.tipo_chip or "Sin tipo",
                    cantidad=int(r.cnt or 0),
                )
                for r in chips_tipo_rows
            ],
            por_monto_recarga=por_monto_recarga,
        ),
        planes=schemas.PlanesStats(
            total=total_planes,
            por_tramite=[
                schemas.TramiteStatItem(
                    tramite=r.tramite or "Sin trámite",
                    cantidad=int(r.cnt or 0),
                )
                for r in planes_tramite_rows
            ],
            por_plan=[
                schemas.PlanStatItem(
                    plan=r.plan or "Sin plan",
                    cantidad=int(r.cnt or 0),
                )
                for r in planes_plan_rows
            ],
            contratos_pendientes=contratos_pendientes,
            contratos_listos=contratos_listos,
        ),
        por_modulo=por_modulo,
        ventas_por_dia=ventas_por_dia,
        telefonos_por_modulo=telefonos_por_modulo,
        telefonos_por_dia=telefonos_por_dia,
        telefonos_top=telefonos_top,
        accesorios_por_dia=accesorios_por_dia,
    )


@router.get("/tiempo-real", response_model=schemas.TiempoRealResponse)
def tiempo_real(
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _verificar_rol(user)

    ahora = datetime.now(ZONA)
    hoy = ahora.date()

    # ── Fecha y hora ──────────────────────────────────────────────────────────
    DIAS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    fecha_texto = f"{DIAS_ES[hoy.weekday()]}, {hoy.day} de {MESES_ES[hoy.month - 1].lower()} de {hoy.year}"
    hora_actual_str = ahora.strftime("%H:%M")

    # ── Horas transcurridas (horario laboral 09:00–21:00) ─────────────────────
    hora_decimal = ahora.hour + ahora.minute / 60.0
    if hora_decimal < 9:
        horas_transcurridas: float = 0.1
    elif hora_decimal >= 21:
        horas_transcurridas = 12.0
    else:
        horas_transcurridas = round(hora_decimal - 9.0, 2)

    horas_totales = 12
    porcentaje_dia = round(horas_transcurridas / horas_totales * 100, 1)

    # ── Filtros base HOY ──────────────────────────────────────────────────────
    f_ventas_hoy = [
        models.Venta.fecha == hoy,
        models.Venta.cancelada.isnot(True),
        func.coalesce(models.Venta.devuelta, False).is_(False),
        ~models.Modulo.nombre.in_(MODULOS_EXCLUIR_SQL),
    ]
    f_ventas_tel_hoy = f_ventas_hoy + [models.Venta.tipo_producto.ilike("telefono")]
    f_ventas_acc_hoy = f_ventas_hoy + [~models.Venta.tipo_producto.ilike("telefono")]

    f_chips_hoy = [
        models.VentaChip.fecha == hoy,
        models.VentaChip.cancelada.isnot(True),
        ~models.Modulo.nombre.in_(MODULOS_EXCLUIR_SQL),
    ]

    # ── Teléfonos hoy ─────────────────────────────────────────────────────────
    tel_tipo_rows = (
        db.query(
            models.Venta.tipo_venta,
            func.count(func.distinct(func.coalesce(models.Venta.folio, func.concat('id-', models.Venta.id)))).label("cnt"),
            func.sum(models.Venta.precio_unitario * models.Venta.cantidad).label("monto"),
        )
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(*f_ventas_tel_hoy)
        .group_by(models.Venta.tipo_venta)
        .all()
    )

    contado: dict = {"cantidad": 0, "monto": 0.0}
    payjoy: dict = {"cantidad": 0, "monto": 0.0}
    paguitos: dict = {"cantidad": 0, "monto": 0.0}
    plan: dict = {"cantidad": 0, "monto": 0.0}
    sin_clasificar: dict = {"cantidad": 0, "monto": 0.0}

    for row in tel_tipo_rows:
        tv = (row.tipo_venta or "").strip().lower()
        cnt = int(row.cnt or 0)
        monto_t = float(row.monto or 0)
        if tv in ("pajoy", "payjoy"):
            payjoy["cantidad"] += cnt
            payjoy["monto"] += monto_t
        elif tv == "paguitos":
            paguitos["cantidad"] += cnt
            paguitos["monto"] += monto_t
        elif tv == "plan":
            # Pago inicial de un plan tarifario: es su propia categoria, no Contado.
            plan["cantidad"] += cnt
            plan["monto"] += monto_t
        elif not tv:
            sin_clasificar["cantidad"] += cnt
            sin_clasificar["monto"] += monto_t
        else:
            contado["cantidad"] += cnt
            contado["monto"] += monto_t

    total_telefonos = (
        contado["cantidad"] + payjoy["cantidad"] + paguitos["cantidad"]
        + plan["cantidad"] + sin_clasificar["cantidad"]
    )

    # ── Accesorios hoy ────────────────────────────────────────────────────────
    acc_agg = (
        db.query(
            func.sum(models.Venta.cantidad).label("total_unidades"),
            func.sum(models.Venta.precio_unitario * models.Venta.cantidad).label("monto_total"),
        )
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(*f_ventas_acc_hoy)
        .first()
    )
    total_unidades_acc = int(acc_agg.total_unidades or 0)
    monto_acc = float(acc_agg.monto_total or 0)

    top5 = (
        db.query(
            models.Venta.producto,
            func.sum(models.Venta.cantidad).label("total_cantidad"),
            func.sum(models.Venta.precio_unitario * models.Venta.cantidad).label("total_monto"),
        )
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(*f_ventas_acc_hoy)
        .group_by(models.Venta.producto)
        .order_by(func.sum(models.Venta.cantidad).desc())
        .limit(5)
        .all()
    )

    total_mxn_row = (
        db.query(func.sum(models.Venta.precio_unitario * models.Venta.cantidad).label("total"))
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(*f_ventas_hoy)
        .first()
    )
    total_ventas_mxn = round(float(total_mxn_row.total or 0), 2)

    # ── Chips hoy ─────────────────────────────────────────────────────────────
    chips_tipo_rows = (
        db.query(
            models.VentaChip.tipo_chip,
            func.count(models.VentaChip.id).label("cnt"),
        )
        .join(models.Usuario, models.VentaChip.empleado_id == models.Usuario.id)
        .join(models.Modulo, models.Usuario.modulo_id == models.Modulo.id)
        .filter(*f_chips_hoy)
        .group_by(models.VentaChip.tipo_chip)
        .order_by(func.count(models.VentaChip.id).desc())
        .all()
    )

    chips_monto_rows = (
        db.query(
            models.VentaChip.monto_recarga,
            func.count(models.VentaChip.id).label("cnt"),
        )
        .join(models.Usuario, models.VentaChip.empleado_id == models.Usuario.id)
        .join(models.Modulo, models.Usuario.modulo_id == models.Modulo.id)
        .filter(*f_chips_hoy)
        .group_by(models.VentaChip.monto_recarga)
        .all()
    )

    total_chips = sum(int(r.cnt or 0) for r in chips_tipo_rows)

    _BUCKET_ORDER_TR = ["$50", "$100", "$150", "$200", "$Otro"]

    def _bucket_tr(mv: float) -> Optional[str]:
        if mv < 50:
            return None
        elif mv < 90:
            return "$50"
        elif mv < 125:
            return "$100"
        elif mv < 175:
            return "$150"
        elif mv < 225:
            return "$200"
        else:
            return "$Otro"

    monto_buckets: dict = defaultdict(int)
    for row in chips_monto_rows:
        lbl = _bucket_tr(float(row.monto_recarga or 0))
        if lbl is not None:
            monto_buckets[lbl] += int(row.cnt or 0)

    por_monto_recarga = [
        schemas.MontoRecargaStatItem(monto=lbl, cantidad=cnt)
        for lbl, cnt in sorted(
            monto_buckets.items(),
            key=lambda x: _BUCKET_ORDER_TR.index(x[0]) if x[0] in _BUCKET_ORDER_TR else 99,
        )
        if cnt > 0
    ]

    # ── Lista de teléfonos vendidos hoy ──────────────────────────────────────
    lista_tel_rows = (
        db.query(
            models.Venta.hora,
            models.Modulo.nombre.label("modulo"),
            models.Usuario.username.label("asesor"),
            models.Venta.producto,
            models.Venta.tipo_venta,
            models.Venta.precio_unitario,
        )
        .select_from(models.Venta)
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .join(models.Usuario, models.Venta.empleado_id == models.Usuario.id)
        .filter(*f_ventas_tel_hoy)
        .order_by(models.Venta.hora.desc())
        .all()
    )

    lista_telefonos_hoy = [
        schemas.TelefonoHoyItem(
            hora=row.hora.strftime("%H:%M") if row.hora else "—",
            modulo=row.modulo or "—",
            asesor=row.asesor or "—",
            producto=row.producto or "—",
            tipo_venta=row.tipo_venta or "—",
            precio=float(row.precio_unitario or 0),
        )
        for row in lista_tel_rows
    ]

    # ── Planes de hoy (PlanTarifario) ─────────────────────────────────────────
    total_planes_hoy = (
        db.query(func.count(models.PlanTarifario.id))
        .join(models.Modulo, models.PlanTarifario.modulo_id == models.Modulo.id)
        .filter(
            func.date(models.PlanTarifario.fecha) == hoy,
            ~models.Modulo.nombre.in_(MODULOS_EXCLUIR_SQL),
        )
        .scalar()
        or 0
    )

    # ── Feed: últimas 10 ventas de hoy (teléfonos + accesorios, sin chips) ─────
    ultimas_ventas_rows = (
        db.query(
            models.Venta.hora,
            models.Modulo.nombre.label("modulo"),
            models.Usuario.username.label("asesor"),
            models.Venta.producto,
            models.Venta.tipo_producto,
            models.Venta.cantidad,
        )
        .select_from(models.Venta)
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .join(models.Usuario, models.Venta.empleado_id == models.Usuario.id)
        .filter(*f_ventas_hoy)
        .order_by(models.Venta.hora.desc())
        .limit(10)
        .all()
    )

    ultimas_ventas = [
        {
            "hora": row.hora.strftime("%H:%M") if row.hora else "—",
            "modulo": row.modulo or "—",
            "asesor": row.asesor or "—",
            "producto": row.producto or "—",
            "tipo": "telefono" if (row.tipo_producto or "").strip().lower() == "telefono" else "accesorio",
            "cantidad": int(row.cantidad or 0),
            "hora_raw": str(row.hora) if row.hora else "",
        }
        for row in ultimas_ventas_rows
    ]

    # ── Por módulo HOY ────────────────────────────────────────────────────────
    modulo_map_tr: dict = defaultdict(lambda: {
        "total_mxn": 0.0,
        "telefonos_contado": 0,
        "telefonos_payjoy": 0,
        "telefonos_paguitos": 0,
        "telefonos_plan": 0,
        "telefonos_total": 0,
        "chips": 0,
        "accesorios": 0,
    })

    for row in (
        db.query(
            models.Modulo.nombre.label("modulo"),
            func.sum(models.Venta.precio_unitario * models.Venta.cantidad).label("monto"),
            func.sum(models.Venta.cantidad).label("unidades"),
        )
        .select_from(models.Venta)
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(*f_ventas_acc_hoy)
        .group_by(models.Modulo.nombre)
        .all()
    ):
        modulo_map_tr[row.modulo]["total_mxn"] += float(row.monto or 0)
        modulo_map_tr[row.modulo]["accesorios"] += int(row.unidades or 0)

    for row in (
        db.query(
            models.Modulo.nombre.label("modulo"),
            models.Venta.tipo_venta,
            func.count(func.distinct(func.coalesce(models.Venta.folio, func.concat('id-', models.Venta.id)))).label("cnt"),
            func.sum(models.Venta.precio_unitario * models.Venta.cantidad).label("monto"),
        )
        .select_from(models.Venta)
        .join(models.Modulo, models.Venta.modulo_id == models.Modulo.id)
        .filter(*f_ventas_tel_hoy)
        .group_by(models.Modulo.nombre, models.Venta.tipo_venta)
        .all()
    ):
        tv = (row.tipo_venta or "").strip().lower()
        cnt = int(row.cnt or 0)
        mv = float(row.monto or 0)
        modulo_map_tr[row.modulo]["total_mxn"] += mv
        modulo_map_tr[row.modulo]["telefonos_total"] += cnt
        if tv in ("pajoy", "payjoy"):
            modulo_map_tr[row.modulo]["telefonos_payjoy"] += cnt
        elif tv == "paguitos":
            modulo_map_tr[row.modulo]["telefonos_paguitos"] += cnt
        elif tv == "plan":
            # Pago inicial de un plan tarifario: es su propia categoria, no Contado.
            modulo_map_tr[row.modulo]["telefonos_plan"] += cnt
        else:
            modulo_map_tr[row.modulo]["telefonos_contado"] += cnt

    for row in (
        db.query(
            models.Modulo.nombre.label("modulo"),
            func.count(models.VentaChip.id).label("cnt"),
        )
        .select_from(models.VentaChip)
        .join(models.Usuario, models.VentaChip.empleado_id == models.Usuario.id)
        .join(models.Modulo, models.Usuario.modulo_id == models.Modulo.id)
        .filter(*f_chips_hoy)
        .group_by(models.Modulo.nombre)
        .all()
    ):
        modulo_map_tr[row.modulo]["chips"] += int(row.cnt or 0)

    # ── Productividad diaria: histórico últimos 6 meses operativos ────────────
    y_h, m_h = hoy.year, hoy.month
    hist_months_tr: list = []
    for _ in range(6):
        m_h -= 1
        if m_h == 0:
            m_h = 12
            y_h -= 1
        hist_months_tr.append((y_h, m_h))

    h_oldest = date(hist_months_tr[-1][0], hist_months_tr[-1][1], 1)
    _, h_newest_ud = calendar.monthrange(hist_months_tr[0][0], hist_months_tr[0][1])
    h_newest_end = date(hist_months_tr[0][0], hist_months_tr[0][1], h_newest_ud)
    hist_months_tr_set = set(hist_months_tr)

    hist_rows_tr = (
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
            models.Venta.fecha >= h_oldest,
            models.Venta.fecha <= h_newest_end,
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

    hist_daily: dict = defaultdict(dict)
    for row in hist_rows_tr:
        key = (int(row.yr), int(row.mo))
        if key in hist_months_tr_set and int(row.cnt or 0) >= 100:
            dias_cal = calendar.monthrange(int(row.yr), int(row.mo))[1]
            hist_daily[row.modulo][key] = {"total": float(row.total or 0), "dias": dias_cal}

    diario_map: dict = {}
    for mod in modulo_map_tr:
        meses = hist_daily.get(mod, {})
        n_meses = len(meses)
        if n_meses == 0:
            diario_map[mod] = {"promedio": 0.0, "meta": 0.0, "pct": None, "dias_considerados": 0}
        else:
            total_mxn_h = sum(v["total"] for v in meses.values())
            total_dias_h = sum(v["dias"] for v in meses.values())
            promedio_diario = total_mxn_h / total_dias_h if total_dias_h > 0 else 0.0
            meta = (promedio_diario * FACTOR_CRECIMIENTO) * (horas_transcurridas / 12.0)
            total_actual = modulo_map_tr[mod]["total_mxn"]
            pct = round((total_actual / meta) * 100, 1) if meta > 0 else None
            diario_map[mod] = {
                "promedio": round(promedio_diario, 2),
                "meta": round(meta, 2),
                "pct": pct,
                "dias_considerados": n_meses,
            }

    por_modulo_tr = sorted(
        [
            schemas.ModuloTiempoRealItem(
                modulo=mod,
                total_mxn=round(vals["total_mxn"], 2),
                telefonos_contado=vals["telefonos_contado"],
                telefonos_payjoy=vals["telefonos_payjoy"],
                telefonos_paguitos=vals["telefonos_paguitos"],
                telefonos_plan=vals["telefonos_plan"],
                telefonos_total=vals["telefonos_total"],
                chips=vals["chips"],
                accesorios=vals["accesorios"],
                promedio_diario_historico=diario_map.get(mod, {}).get("promedio", 0.0),
                meta_proporcional=diario_map.get(mod, {}).get("meta", 0.0),
                productividad_pct=diario_map.get(mod, {}).get("pct"),
                dias_considerados=diario_map.get(mod, {}).get("dias_considerados", 0),
            )
            for mod, vals in modulo_map_tr.items()
        ],
        key=lambda x: x.total_mxn,
        reverse=True,
    )

    # ── Respuesta ─────────────────────────────────────────────────────────────
    return schemas.TiempoRealResponse(
        fecha=str(hoy),
        fecha_texto=fecha_texto,
        hora_actual=hora_actual_str,
        horas_transcurridas=horas_transcurridas,
        horas_totales=horas_totales,
        porcentaje_dia=porcentaje_dia,
        resumen_general=schemas.TiempoRealResumen(
            total_ventas_mxn=total_ventas_mxn,
            total_telefonos=total_telefonos,
            total_chips=total_chips,
            total_accesorios=total_unidades_acc,
        ),
        telefonos=schemas.TelefonosStats(
            total=total_telefonos,
            contado=schemas.CantidadMonto(cantidad=contado["cantidad"], monto=round(contado["monto"], 2)),
            payjoy=schemas.CantidadMonto(cantidad=payjoy["cantidad"], monto=round(payjoy["monto"], 2)),
            paguitos=schemas.CantidadMonto(cantidad=paguitos["cantidad"], monto=round(paguitos["monto"], 2)),
            plan=schemas.CantidadMonto(cantidad=plan["cantidad"], monto=round(plan["monto"], 2)),
            sin_clasificar=schemas.CantidadMonto(cantidad=sin_clasificar["cantidad"], monto=round(sin_clasificar["monto"], 2)),
        ),
        chips=schemas.ChipsStats(
            total=total_chips,
            por_tipo=[
                schemas.TipoChipStatItem(tipo_chip=r.tipo_chip or "Sin tipo", cantidad=int(r.cnt or 0))
                for r in chips_tipo_rows
            ],
            por_monto_recarga=por_monto_recarga,
        ),
        accesorios=schemas.AccesoriosStats(
            total_unidades=total_unidades_acc,
            monto_total=round(monto_acc, 2),
            top_5_productos=[
                schemas.TopProductoItem(
                    producto=r.producto,
                    cantidad=int(r.total_cantidad or 0),
                    monto=round(float(r.total_monto or 0), 2),
                )
                for r in top5
            ],
        ),
        lista_telefonos_hoy=lista_telefonos_hoy,
        por_modulo=por_modulo_tr,
        total_planes_hoy=total_planes_hoy,
        ultimas_ventas=ultimas_ventas,
    )


# ─── RECARGAS ────────────────────────────────────────────────────────────────

@router.get("/recargas-pendientes", response_model=List[schemas.RecargaItemResponse])
def recargas_pendientes(
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _verificar_rol(user)
    cortes = (
        db.query(models.CorteDia)
        .options(joinedload(models.CorteDia.modulo))
        .filter(
            models.CorteDia.revisado_direccion == True,   # noqa: E712
            models.CorteDia.recarga_revisada == False,    # noqa: E712
            (
                func.coalesce(models.CorteDia.adicional_recargas, 0) +
                func.coalesce(models.CorteDia.adicional_transporte, 0) +
                func.coalesce(models.CorteDia.adicional_otros, 0) +
                func.coalesce(models.CorteDia.adicional_mayoreo, 0)
            ) > 0,
        )
        .order_by(models.CorteDia.fecha.desc())
        .all()
    )
    return [
        schemas.RecargaItemResponse(
            id=c.id,
            modulo_id=c.modulo_id,
            modulo_nombre=c.modulo.nombre if c.modulo else "",
            fecha=c.fecha,
            adicional_recargas=c.adicional_recargas or 0,
            adicional_transporte=c.adicional_transporte or 0,
            adicional_otros=c.adicional_otros or 0,
            adicional_mayoreo=c.adicional_mayoreo or 0,
            adicional_mayoreo_para=c.adicional_mayoreo_para or None,
            recarga_revisada=False,
        )
        for c in cortes
    ]


@router.get("/recargas-revisadas", response_model=List[schemas.RecargaItemResponse])
def recargas_revisadas(
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _verificar_rol(user)
    cortes = (
        db.query(models.CorteDia)
        .options(joinedload(models.CorteDia.modulo))
        .filter(
            models.CorteDia.revisado_direccion == True,   # noqa: E712
            models.CorteDia.recarga_revisada == True,     # noqa: E712
        )
        .order_by(models.CorteDia.fecha.desc())
        .all()
    )
    return [
        schemas.RecargaItemResponse(
            id=c.id,
            modulo_id=c.modulo_id,
            modulo_nombre=c.modulo.nombre if c.modulo else "",
            fecha=c.fecha,
            adicional_recargas=c.adicional_recargas or 0,
            adicional_transporte=c.adicional_transporte or 0,
            adicional_otros=c.adicional_otros or 0,
            adicional_mayoreo=c.adicional_mayoreo or 0,
            adicional_mayoreo_para=c.adicional_mayoreo_para or None,
            recarga_revisada=True,
        )
        for c in cortes
    ]


@router.put("/cortes/{corte_id}/marcar-recarga-revisada")
def marcar_recarga_revisada(
    corte_id: int,
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _verificar_rol(user)
    corte = db.query(models.CorteDia).filter(models.CorteDia.id == corte_id).first()
    if not corte:
        raise HTTPException(404, "Corte no encontrado")
    corte.recarga_revisada = True
    db.commit()
    return {"ok": True}


@router.put("/cortes/{corte_id}/desmarcar-recarga-revisada")
def desmarcar_recarga_revisada(
    corte_id: int,
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _verificar_rol(user)
    corte = db.query(models.CorteDia).filter(models.CorteDia.id == corte_id).first()
    if not corte:
        raise HTTPException(404, "Corte no encontrado")
    corte.recarga_revisada = False
    db.commit()
    return {"ok": True}


@router.put("/cortes/{corte_id}/editar-recargas")
def editar_recargas(
    corte_id: int,
    body: schemas.EditarRecargasBody,
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _verificar_rol(user)
    corte = db.query(models.CorteDia).filter(models.CorteDia.id == corte_id).first()
    if not corte:
        raise HTTPException(404, "Corte no encontrado")
    corte.adicional_recargas = body.adicional_recargas
    corte.adicional_transporte = body.adicional_transporte
    corte.adicional_otros = body.adicional_otros
    corte.adicional_mayoreo = body.adicional_mayoreo
    db.commit()
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# Helper: genera el PDF del reporte diario (sin verificación de rol/JWT)
# ─────────────────────────────────────────────────────────────────────────────
def _generar_pdf_reporte(fecha: date, db: Session):
    from io import BytesIO
    from fastapi.responses import StreamingResponse
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.colors import HexColor, white, black

    EXCLUIR_IDS  = {21, 7}
    EFECTIVO_SET = {"efectivo", "cash"}

    modulos = (
        db.query(models.Modulo)
        .filter(models.Modulo.activo == True)  # noqa: E712
        .order_by(models.Modulo.nombre.asc())
        .all()
    )

    resultados = []
    for m in modulos:
        if m.id in EXCLUIR_IDS:
            continue

        corte = (
            db.query(models.CorteDia)
            .filter(
                models.CorteDia.fecha == fecha,
                models.CorteDia.modulo_id == m.id,
            )
            .first()
        )

        ventas_db = []
        if corte:
            ventas_db = (
                db.query(models.Venta)
                .filter(
                    models.Venta.fecha == fecha,
                    models.Venta.modulo_id == m.id,
                    models.Venta.cancelada.isnot(True),
                )
                .all()
            )

        tel_por_prod: dict = {}
        acc_por_prod: dict = {}
        tel_ef  = 0.0
        tel_tar = 0.0

        for v in ventas_db:
            tipo  = (v.tipo_producto or "").lower().strip()
            monto = float((v.precio_unitario or 0) * (v.cantidad or 0))
            if tipo == "telefono":
                pago = (v.metodo_pago or "").lower().strip()
                if pago in EFECTIVO_SET:
                    tel_ef  += monto
                else:
                    tel_tar += monto
                ent = tel_por_prod.setdefault(v.producto or "Sin nombre", {"cant": 0, "total": 0.0})
                ent["cant"]  += int(v.cantidad or 0)
                ent["total"] += monto
            elif tipo == "accesorios":
                ent = acc_por_prod.setdefault(v.producto or "Sin nombre", {"cant": 0, "total": 0.0})
                ent["cant"]  += int(v.cantidad or 0)
                ent["total"] += monto

        tel_tot = tel_ef + tel_tar
        acc_ef  = float(corte.accesorios_efectivo) if corte else 0.0
        acc_tar = float(corte.accesorios_tarjeta)  if corte else 0.0
        acc_tot = float(corte.accesorios_total)    if corte else 0.0
        mod_ef  = acc_ef + tel_ef
        mod_tar = acc_tar + tel_tar
        mod_tot = acc_tot + tel_tot

        resultados.append({
            "nombre":       m.nombre,
            "sin_ventas":   corte is None or (acc_tot == 0 and tel_tot == 0),
            "tel_por_prod": tel_por_prod,
            "acc_por_prod": acc_por_prod,
            "tel_ef": tel_ef,  "tel_tar": tel_tar,  "tel_tot": tel_tot,
            "acc_ef": acc_ef,  "acc_tar": acc_tar,  "acc_tot": acc_tot,
            "mod_ef": mod_ef,  "mod_tar": mod_tar,  "mod_tot": mod_tot,
        })

    # ── Resumen general ───────────────────────────────────────────────────────
    res_tel = sum(r["tel_tot"] for r in resultados)
    res_acc = sum(r["acc_tot"] for r in resultados)
    res_tot = sum(r["mod_tot"] for r in resultados)
    total_unidades_tel = sum(
        d["cant"] for r in resultados for d in r["tel_por_prod"].values()
    )

    # ── Lista global de teléfonos (para la sección "TELÉFONOS VENDIDOS HOY") ─
    tel_global = []
    for r in resultados:
        for modelo, datos in sorted(r["tel_por_prod"].items()):
            tel_global.append({
                "modelo": modelo,
                "modulo": r["nombre"],
                "cant":   datos["cant"],
                "total":  datos["total"],
            })

    # ── Generar PDF ───────────────────────────────────────────────────────────
    AZUL     = HexColor("#16264a")
    AMARILLO = HexColor("#f5c542")
    VERDE    = HexColor("#1e7a46")
    NARANJA  = HexColor("#e07b1a")
    GRIS_F   = HexColor("#f0f4fa")
    GRIS_T   = HexColor("#dde5f0")

    W       = 612.0
    MARGEN  = 30
    ancho   = W - 2 * MARGEN

    CP = MARGEN + 4
    CC = W - MARGEN - 110
    CT = W - MARGEN - 4

    ROW_H   = 16
    HDR_H   = 20
    BANDA_H = 26
    SUB_H   = 22
    BOX_H   = 85.0
    BAR_H   = 30
    BOX_W   = (ancho - 10) / 2

    TELGLOBAL_H = HDR_H + (len(tel_global) * ROW_H if tel_global else 22) + 10
    INTRO_H = 14 + 10 + (BOX_H + 6) + (BAR_H + 12) + TELGLOBAL_H
    content_h = float(INTRO_H)
    for r in resultados:
        content_h += BANDA_H + 4
        if r["sin_ventas"]:
            content_h += 22
        else:
            if r["tel_por_prod"]:
                content_h += HDR_H + len(r["tel_por_prod"]) * ROW_H
            if r["acc_por_prod"]:
                content_h += HDR_H + len(r["acc_por_prod"]) * ROW_H
            content_h += SUB_H + 26

    H = 82.0 + content_h + MARGEN

    def fp(v: float) -> str:
        return f"${v:,.2f}"

    buf       = BytesIO()
    c         = rl_canvas.Canvas(buf, pagesize=(W, H))
    fecha_str = fecha.strftime("%d / %m / %Y")

    def hdr() -> None:
        c.setFillColor(AZUL)
        c.rect(0, H - 70, W, 70, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 18)
        c.drawString(MARGEN, H - 32, "ATO Sistema")
        c.setFont("Helvetica", 11)
        c.drawString(MARGEN, H - 52, "Reporte de ventas")
        c.setFillColor(AMARILLO)
        c.setFont("Helvetica-Bold", 14)
        c.drawRightString(W - MARGEN, H - 40, fecha_str)

    def tabla_hdr(y: float, titulo: str, bg=None, fg=None) -> float:
        c.setFillColor(bg if bg is not None else GRIS_T)
        c.rect(MARGEN, y - HDR_H, ancho, HDR_H, fill=1, stroke=0)
        c.setFillColor(fg if fg is not None else AZUL)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(CP, y - 13, titulo)
        c.drawRightString(CC, y - 13, "Cant.")
        c.drawRightString(CT, y - 13, "Total")
        return y - HDR_H

    def tabla_row(y: float, prod: str, datos: dict, idx: int) -> float:
        c.setFillColor(GRIS_F if idx % 2 == 0 else white)
        c.rect(MARGEN, y - ROW_H, ancho, ROW_H, fill=1, stroke=0)
        c.setFillColor(black)
        c.setFont("Helvetica", 13)
        c.drawString(CP, y - 11, prod[:55])
        c.drawRightString(CC, y - 11, str(datos["cant"]))
        c.drawRightString(CT, y - 11, fp(datos["total"]))
        return y - ROW_H

    hdr()
    y = float(H - 82)

    y -= 14
    c.setFillColor(AZUL)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(MARGEN, y, "RESUMEN GENERAL DEL DÍA")
    y -= 10

    for i, (lbl, val) in enumerate([
        ("Teléfonos", res_tel), ("Accesorios", res_acc),
    ]):
        bx = MARGEN + i * (BOX_W + 10)
        by = y - BOX_H
        c.setFillColor(GRIS_F)
        c.rect(bx, by, BOX_W, BOX_H, fill=1, stroke=0)
        c.setFillColor(AZUL)
        c.setFont("Helvetica-Bold", 13)
        c.drawCentredString(bx + BOX_W / 2, by + BOX_H - 18, lbl)
        c.setFont("Helvetica-Bold", 20)
        c.drawCentredString(bx + BOX_W / 2, by + 36, fp(val))
        if lbl == "Teléfonos":
            c.setFillColor(VERDE)
            c.setFont("Helvetica-Bold", 12)
            c.drawCentredString(bx + BOX_W / 2, by + 12, f"{total_unidades_tel} equipos")
    y -= BOX_H + 6

    c.setFillColor(AMARILLO)
    c.rect(MARGEN, y - BAR_H, ancho, BAR_H, fill=1, stroke=0)
    c.setFillColor(AZUL)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(MARGEN + 8, y - 23, "TOTAL GENERAL DEL DÍA")
    c.drawRightString(CT, y - 23, fp(res_tot))
    y -= BAR_H + 12

    # ── VENTAS POR MÓDULO (cuadrícula 5 por fila) ─────────────────────────────
    # Barra de título
    c.setFillColor(AZUL)
    c.rect(MARGEN, y - HDR_H, ancho, HDR_H, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(CP, y - 13, "VENTAS POR MÓDULO")
    y -= HDR_H + 8

    COLS    = 5
    GAP     = 6
    CARD_W  = (ancho - GAP * (COLS - 1)) / COLS
    CARD_H  = 56
    for i, r in enumerate(resultados):
        col = i % COLS
        if col == 0 and i != 0:
            y -= CARD_H + GAP
        x = MARGEN + col * (CARD_W + GAP)
        # cantidad de teléfonos del módulo
        n_tel = sum(d["cant"] for d in r["tel_por_prod"].values())
        # caja
        c.setFillColor(GRIS_F)
        c.rect(x, y - CARD_H, CARD_W, CARD_H, fill=1, stroke=0)
        c.setStrokeColor(GRIS_T)
        c.setLineWidth(0.5)
        c.rect(x, y - CARD_H, CARD_W, CARD_H, fill=0, stroke=1)
        # nombre del módulo
        c.setFillColor(AZUL)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x + 6, y - 14, str(r["nombre"])[:14])
        # datos
        c.setFillColor(black)
        c.setFont("Helvetica", 8)
        c.drawString(x + 6, y - 27, "Teléfonos:")
        c.drawRightString(x + CARD_W - 6, y - 27, str(n_tel))
        c.drawString(x + 6, y - 38, "$ Tel:")
        c.drawRightString(x + CARD_W - 6, y - 38, fp(r["tel_tot"]))
        c.drawString(x + 6, y - 49, "$ Acc:")
        c.drawRightString(x + CARD_W - 6, y - 49, fp(r["acc_tot"]))
    y -= CARD_H + 18

    # ── TELÉFONOS VENDIDOS HOY ────────────────────────────────────────────────
    CM_GBL = MARGEN + 230   # módulo — left-align
    c.setFillColor(VERDE)
    c.rect(MARGEN, y - HDR_H, ancho, HDR_H, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(CP, y - 13, "TELÉFONOS VENDIDOS HOY")
    c.drawString(CM_GBL, y - 13, "MÓDULO")
    c.drawRightString(CT, y - 13, "PRECIO")
    y -= HDR_H

    if not tel_global:
        c.setFillColor(black)
        c.setFont("Helvetica-Oblique", 11)
        c.drawString(MARGEN + 8, y - 14, "Sin teléfonos vendidos hoy")
        y -= 22
    else:
        for idx, item in enumerate(tel_global):
            c.setFillColor(GRIS_F if idx % 2 == 0 else white)
            c.rect(MARGEN, y - ROW_H, ancho, ROW_H, fill=1, stroke=0)
            c.setFillColor(black)
            c.setFont("Helvetica", 12)
            modelo_txt = (
                item["modelo"][:24] + f" (x{item['cant']})"
                if item["cant"] > 1
                else item["modelo"][:30]
            )
            c.drawString(CP, y - 11, modelo_txt)
            c.drawString(CM_GBL, y - 11, item["modulo"][:22])
            c.drawRightString(CT, y - 11, fp(item["total"]))
            y -= ROW_H

    y -= 10

    for r in resultados:
        c.setFillColor(AZUL)
        c.rect(MARGEN, y - BANDA_H, ancho, BANDA_H, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(MARGEN + 6, y - BANDA_H + 8, r["nombre"].upper())
        y -= BANDA_H + 4

        if r["sin_ventas"]:
            c.setFillColor(black)
            c.setFont("Helvetica-Oblique", 11)
            c.drawString(MARGEN + 8, y - 14, "Sin ventas")
            y -= 22
            continue

        if r["tel_por_prod"]:
            y = tabla_hdr(y, "TELÉFONOS", bg=VERDE, fg=white)
            for idx, (prod, datos) in enumerate(sorted(r["tel_por_prod"].items())):
                y = tabla_row(y, prod, datos, idx)

        if r["acc_por_prod"]:
            y = tabla_hdr(y, "ACCESORIOS", bg=NARANJA, fg=white)
            for idx, (prod, datos) in enumerate(sorted(r["acc_por_prod"].items())):
                y = tabla_row(y, prod, datos, idx)

        c.setFillColor(GRIS_T)
        c.rect(MARGEN, y - SUB_H, ancho, SUB_H, fill=1, stroke=0)
        c.setFillColor(AZUL)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(MARGEN + 4,   y - 15, "Teléfonos:")
        c.setFillColor(black)
        c.setFont("Helvetica", 12)
        c.drawRightString(MARGEN + 165, y - 15, fp(r["tel_tot"]))
        c.setFillColor(AZUL)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(MARGEN + 175, y - 15, "Accesorios:")
        c.setFillColor(black)
        c.setFont("Helvetica", 12)
        c.drawRightString(MARGEN + 330, y - 15, fp(r["acc_tot"]))
        c.setFillColor(AZUL)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(MARGEN + 340, y - 15, "Total de la venta:")
        c.setFillColor(black)
        c.setFont("Helvetica-Bold", 12)
        c.drawRightString(CT, y - 15, fp(r["mod_tot"]))
        y -= SUB_H + 26

    c.save()
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="reporte_{fecha}.pdf"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /direccion/reporte-diario/pdf  (protegido por JWT)
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/reporte-diario/pdf")
def reporte_diario_pdf(
    fecha: date = Query(...),
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _verificar_rol(user)
    return _generar_pdf_reporte(fecha, db)


# ─────────────────────────────────────────────────────────────────────────────
# GET /direccion/reporte-diario/pdf-publico  (sin JWT, protegido por llave)
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/reporte-diario/pdf-publico")
def reporte_diario_pdf_publico(
    fecha: date = Query(...),
    key: str = Query(...),
    db: Session = Depends(get_db),
):
    import os
    expected = os.environ.get("REPORTE_PDF_KEY", "")
    if not expected or key != expected:
        raise HTTPException(status_code=403, detail="No autorizado")
    return _generar_pdf_reporte(fecha, db)


# ─────────────────────────────────────────────────────────────────────────────
# POST /direccion/enviar-reporte-whatsapp  (protegido por llave, sin JWT)
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/enviar-reporte-whatsapp")
def enviar_reporte_whatsapp(
    fecha: date = Query(default=None),
    key: str = Query(...),
):
    import os
    import json
    from datetime import datetime

    expected = os.environ.get("REPORTE_PDF_KEY", "")
    if not expected or key != expected:
        raise HTTPException(status_code=403, detail="No autorizado")

    if fecha is None:
        fecha = datetime.now(ZONA).date()

    account_sid   = os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth_token    = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_number   = os.environ.get("TWILIO_WHATSAPP_FROM", "")
    to_number     = os.environ.get("REPORTE_WHATSAPP_TO", "")
    template_sid  = os.environ.get("TWILIO_TEMPLATE_SID", "")
    pdf_key       = os.environ.get("REPORTE_PDF_KEY", "")

    fecha_ddmmyyyy = fecha.strftime("%d/%m/%Y")
    pdf_link = (
        f"https://ato-appservidor-nvxt.onrender.com"
        f"/direccion/reporte/{fecha}.pdf"
        f"?key={pdf_key}"
    )
    content_variables = json.dumps({"1": fecha_ddmmyyyy, "2": pdf_link})

    media_url = (
        f"https://ato-appservidor-nvxt.onrender.com"
        f"/direccion/reporte/{fecha}.pdf"
        f"?key={pdf_key}"
    )

    # REPORTE_WHATSAPP_TO puede traer varios números separados por coma.
    # Ej: "whatsapp:+5214495131043,whatsapp:+5214491440784"
    destinatarios = [n.strip() for n in to_number.split(",") if n.strip()]

    from twilio.rest import Client
    client = Client(account_sid, auth_token)

    enviados = []
    errores = []
    for to in destinatarios:
        try:
            msg = client.messages.create(
                from_=from_number,
                to=to,
                content_sid=template_sid,
                content_variables=content_variables,
            )
            enviados.append({"to": to, "sid": msg.sid})
        except Exception as e:
            # Si uno falla, registramos su error y seguimos con los demás.
            errores.append({"to": to, "error": str(e)})

    return {"ok": True, "enviados": enviados, "errores": errores}


# ─────────────────────────────────────────────────────────────────────────────
# GET /direccion/reporte/{fecha}.pdf  — URL pública con extensión .pdf
# compatible con WhatsApp/Twilio media_url (debe terminar en extensión reconocida)
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/reporte/{fecha}.pdf")
def reporte_pdf_publico_ext(
    fecha: str,
    key: str = Query(...),
    db: Session = Depends(get_db),
):
    import os
    from datetime import date as _date

    expected = os.environ.get("REPORTE_PDF_KEY", "")
    if not expected or key != expected:
        raise HTTPException(status_code=403, detail="Clave inválida")

    try:
        fecha_date = _date.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(status_code=400, detail="Fecha inválida, use formato YYYY-MM-DD")

    return _generar_pdf_reporte(fecha_date, db)
