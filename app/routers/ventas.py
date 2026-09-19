
from datetime import date, datetime, time, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import func, text
from sqlalchemy.orm import Session, joinedload, selectinload
from app import models, schemas
from app.database import get_db
from app.routers.usuarios import get_current_user
from app.utilidades import calcular_comision_telefono, enviar_ticket, mismo_folio, verificar_rol_requerido, verificar_modulo_no_congelado
from datetime import date
from app.routers.kardex import registrar_kardex
from app.routers.planes_tarifarios import revertir_plan
import os
from supabase import create_client

def _get_supabase():
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        return None
    return create_client(url, key)



router = APIRouter()


zona_horaria = ZoneInfo("America/Mexico_City")


@router.post("/ventas", response_model=List[schemas.VentaResponse])
def crear_ventas(
    venta: schemas.VentaMultipleCreate,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):
    ventas_realizadas = []

    # Si el request trae un folio (ej. segunda llamada de pago dividido), reusarlo.
    # Si no, generar uno nuevo (excepto Cadenas id=7).
    if venta.folio:
        folio_venta = venta.folio
    elif current_user.modulo_id != 7:
        seq = db.execute(text("SELECT nextval('venta_folio_seq')")).scalar()
        folio_venta = f"V-{seq}"
    else:
        folio_venta = None

    for item in venta.productos:
        com = (
            db.query(models.Comision)
            .filter(func.lower(models.Comision.producto) == item.producto.strip().lower())
            .first()
        )
        sin_comision   = item.skip_comision or (item.tipo_producto != 'telefono' and item.precio_unitario < 30)
        comision_id    = None if sin_comision else (com.id if com else None)
        comision_monto = None if sin_comision else (com.cantidad if com else None)
        modulo_id = current_user.modulo_id
        verificar_modulo_no_congelado(db, modulo_id)

        if not item.skip_inventario:
            inventario = (
                db.query(models.InventarioModulo)
                .filter(
                    models.InventarioModulo.modulo_id == modulo_id,
                    models.InventarioModulo.producto == item.producto,
                    models.InventarioModulo.tipo_producto == item.tipo_producto
                )
                .first()
            )

            if not inventario:
                raise HTTPException(400, f"No hay inventario para {item.producto}")

            if inventario.cantidad < item.cantidad:
                raise HTTPException(400, f"Inventario insuficiente para {item.producto}")

            inventario.cantidad -= item.cantidad

        fecha_actual = datetime.now(zona_horaria)

        # ✅ VALIDACIÓN CORRECTA
        if item.tipo_producto == "telefono":
            if not item.chip_casado:
                raise HTTPException(
                    status_code=400,
                    detail=f"chip_casado es obligatorio para teléfonos: {item.producto}"
                )

        total = item.precio_unitario * item.cantidad

        nueva = models.Venta(
            empleado_id=current_user.id,
            modulo_id=modulo_id,
            producto=item.producto,
            cantidad=item.cantidad,
            precio_unitario=item.precio_unitario,
            tipo_producto=item.tipo_producto,
            tipo_venta=item.tipo_venta,
            metodo_pago=venta.metodo_pago,
            total=total,
            cancelada=False,
            comision_id=comision_id,
            comision_monto=comision_monto,
            chip_casado=item.chip_casado,
            fecha=fecha_actual.date(),
            hora=fecha_actual.time(),
            telefono_cliente=venta.telefono_cliente,
            folio=folio_venta,
            imei=item.imei,
            clasificacion=item.clasificacion,
        )

        db.add(nueva)
        db.flush()

        if not item.skip_inventario:
            registrar_kardex(
                db=db,
                producto=nueva.producto,
                tipo_producto=nueva.tipo_producto,
                cantidad=nueva.cantidad,
                tipo_movimiento="VENTA",
                usuario_id=current_user.id,
                modulo_origen_id=modulo_id,
                referencia_id=nueva.id
            )

        # Validar y marcar equipo Telcel como vendido (solo si el teléfono trae imei)
        if item.tipo_producto == "telefono" and item.imei:
            imei_limpio = str(item.imei).strip()
            equipo = (
                db.query(models.EquiposTelcel)
                .filter(models.EquiposTelcel.imei == imei_limpio)
                .first()
            )
            if not equipo:
                raise HTTPException(status_code=400, detail=f"El IMEI {imei_limpio} no está registrado")
            if equipo.estatus == "vendido":
                raise HTTPException(status_code=400, detail=f"El IMEI {imei_limpio} ya fue vendido")
            if equipo.estatus != "surtido":
                raise HTTPException(status_code=400, detail=f"El IMEI {imei_limpio} no está surtido a un módulo (estatus: {equipo.estatus})")
            if equipo.modulo_id != modulo_id:
                raise HTTPException(status_code=400, detail=f"El IMEI {imei_limpio} no pertenece a este módulo")
            # Marcar como vendido
            equipo.estatus = "vendido"
            equipo.fecha_venta = fecha_actual.replace(tzinfo=None)
            equipo.folio_venta = folio_venta

        ventas_realizadas.append(nueva)

    db.commit()
    for v in ventas_realizadas:
        db.refresh(v)

    # --- UPSET/ACTUALIZACIÓN de CorteDia ---
    try:
        # obtener o crear corte para la fecha y módulo
        fecha_corte = fecha_actual.date()
        modulo_id = current_user.modulo.id

        corte = db.query(models.CorteDia).filter(
            models.CorteDia.fecha == fecha_corte,
            models.CorteDia.modulo_id == modulo_id
        ).first()

        if not corte:
            corte = models.CorteDia(
                fecha=fecha_corte,
                modulo_id=modulo_id,
                total_efectivo=0.0,
                total_tarjeta=0.0,
                adicional_recargas=0.0,
                adicional_transporte=0.0,
                adicional_otros=0.0,
                total_sistema=0.0,
                total_general=0.0,
                accesorios_efectivo=0.0,
                accesorios_tarjeta=0.0,
                accesorios_total=0.0,
                telefonos_efectivo=0.0,
                telefonos_tarjeta=0.0,
                telefonos_total=0.0
            )
            db.add(corte)
            db.flush()  # asegura que corte tenga id si es necesario

        # Sumarizar las ventas realizadas en este request al corte
        suma_request = 0.0
        for v in ventas_realizadas:
            pago = (v.metodo_pago or "").strip().lower()
            es_efectivo = pago == "efectivo" or pago == "cash"  # ajusta si tienes otros valores
            total_v = float(v.total or 0)

            if es_efectivo:
                corte.total_efectivo = (corte.total_efectivo or 0) + total_v
                if v.tipo_producto == "accesorios":
                    corte.accesorios_efectivo = (corte.accesorios_efectivo or 0) + total_v
                else:
                    corte.telefonos_efectivo = (corte.telefonos_efectivo or 0) + total_v
            else:
                # todo lo que no sea "efectivo" lo acumulamos en tarjeta (ajusta según necesites)
                corte.total_tarjeta = (corte.total_tarjeta or 0) + total_v
                if v.tipo_producto == "accesorios":
                    corte.accesorios_tarjeta = (corte.accesorios_tarjeta or 0) + total_v
                else:
                    corte.telefonos_tarjeta = (corte.telefonos_tarjeta or 0) + total_v

            # totales por tipo
            if v.tipo_producto == "accesorios":
                corte.accesorios_total = (corte.accesorios_total or 0) + total_v
            else:
                corte.telefonos_total = (corte.telefonos_total or 0) + total_v

            # acumulador para total sistema y general
            corte.total_sistema = (corte.total_sistema or 0) + total_v
            suma_request += total_v

        # Actualizar total_general (si quieres incluir ya las adicionales, agrégalas aquí)
        corte.total_general = (corte.total_general or 0) + suma_request

        db.commit()
        # opcional: db.refresh(corte)
    except Exception as e:
        # No queremos romper la respuesta si falla la actualización del corte,
        # pero sí devolvemos información del error en logs.
        db.rollback()
        # loguear e.g. logger.error(...) si tienes logger
        print("Error actualizando CorteDia:", e)

    return [
        schemas.VentaResponse(
            id=v.id,
            empleado=schemas.UsuarioResponse.from_orm(v.empleado) if v.empleado else None,
            modulo=v.modulo,
            producto=v.producto,
            cantidad=v.cantidad,
            precio_unitario=v.precio_unitario,
            metodo_pago=v.metodo_pago, 
            chip_casado=v.chip_casado,
            total=v.precio_unitario * v.cantidad,
            comision=(v.comision_monto if v.comision_monto is not None
                      else (db.query(models.Comision).filter_by(id=v.comision_id).first().cantidad
                            if v.comision_id else None)),
            fecha=v.fecha,
            hora=v.hora,
            cancelada=v.cancelada,
            folio=v.folio,
        )
        for v in ventas_realizadas
    ]



# ------------------- VENTAS -------------------
# @router.post("/ventas", response_model=schemas.VentaResponse)
# def crear_venta(venta: schemas.VentaCreate, db: Session = Depends(get_db), current_user: models.Usuario = Depends(get_current_user)):
    
#     com = (
#         db.query(models.Comision)
#           .filter(func.lower(models.Comision.producto) == venta.producto.strip().lower())
#           .first()
#     )
#     comision = com.cantidad if com else None

    
#     total = venta.precio_unitario * venta.cantidad

#     modulo = current_user.modulo
    
    
#     inventario = (
#         db.query(models.InventarioModulo)
#         .filter_by(modulo=modulo, producto=venta.producto)
#         .first()
#     )

#     if not inventario:
#         raise HTTPException(status_code=404, detail="Producto no registrado en el inventario del módulo")

#     if inventario.cantidad < venta.cantidad:
#         raise HTTPException(status_code=400, detail="Inventario insuficiente para esta venta")

#     fecha_actual = datetime.now(zona_horaria)
#     inventario.cantidad -= venta.cantidad
    
#     # 3. Crear la venta
#     nueva_venta = models.Venta(
#         empleado_id=current_user.id,
#         modulo=modulo,
#         producto=venta.producto,
#         cantidad=venta.cantidad,
#         precio_unitario=venta.precio_unitario,
#         total = venta.precio_unitario * venta.cantidad,
#         comision=comision,
#         fecha=fecha_actual.date(),
#         hora=fecha_actual.time(),
#         correo_cliente=venta.correo_cliente
#     )
#     # Si dejaste el campo total en el modelo, descomenta esta línea:
#     # nueva_venta.total = total

#     db.add(nueva_venta)
#     db.commit()
#     db.refresh(nueva_venta)
    
    
#     try:
#         enviar_ticket(venta.correo_cliente, {
#             "producto": venta.producto,
#             "cantidad": venta.cantidad,
#             "total": nueva_venta.total
#         })
#     except Exception as e:
#         print("Error al enviar correo:", e)


#     respuesta = schemas.VentaResponse.from_orm(nueva_venta)
#     respuesta.total = total        
#     return respuesta

    


from datetime import date

from datetime import datetime, date

@router.get("/ventas", response_model=list[schemas.VentaResponse])
def obtener_ventas(
    fecha: date = None,
    modulo_id: int = None,
    folio: str = None,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)  # quien hizo login
):
    hoy = datetime.now(zona_horaria).date()

    query = (
        db.query(models.Venta)
        .options(joinedload(models.Venta.empleado))
    )

    # Si viene folio y NO viene fecha, no forzar filtro de fecha (el folio puede ser de cualquier día)
    if fecha:
        query = query.filter(models.Venta.fecha == fecha)
    elif not folio:
        query = query.filter(models.Venta.fecha == hoy)

    if folio:
        query = query.filter(models.Venta.folio == folio)

    # 🔒 Si no es admin, solo puede ver su propio módulo
    if not current_user.is_admin:
        query = query.filter(models.Venta.modulo_id == current_user.modulo_id)
    else:
        # si es admin y mandó modulo_id → filtrar
        if modulo_id is not None:
            query = query.filter(models.Venta.modulo_id == modulo_id)

    ventas = query.all()

    resultados = []
    for v in ventas:
        item = schemas.VentaResponse.from_orm(v)
        item.total = v.precio_unitario * v.cantidad
        print("Debug venta:", item.dict())
        resultados.append(item)

    return resultados


@router.get("/ventas_telcel")
def obtener_ventas_telcel(
    fecha_inicio: str = None,
    fecha_fin: str = None,
    modulo_id: int = None,
    imei: str = None,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(verificar_rol_requerido(models.RolEnum.admin))
):
    query = (
        db.query(models.Venta)
        .options(joinedload(models.Venta.empleado), joinedload(models.Venta.modulo))
        .filter(models.Venta.clasificacion.isnot(None))
        .filter(models.Venta.tipo_producto == 'telefono')
        .filter(models.Venta.cancelada == False)
    )
    if fecha_inicio:
        query = query.filter(models.Venta.fecha >= fecha_inicio)
    if fecha_fin:
        query = query.filter(models.Venta.fecha <= fecha_fin)
    if modulo_id is not None:
        query = query.filter(models.Venta.modulo_id == modulo_id)
    if imei:
        query = query.filter(models.Venta.imei.ilike(f"%{imei.strip()}%"))

    ventas = query.order_by(models.Venta.id.desc()).all()

    return [
        {
            "id": v.id,
            "folio": v.folio,
            "producto": v.producto,
            "precio": v.precio_unitario,
            "imei": v.imei,
            "clasificacion": v.clasificacion,
            "numero": v.chip_casado,
            "tipo_venta": v.tipo_venta,
            "metodo_pago": v.metodo_pago,
            "vendedor": v.empleado.username if v.empleado else None,
            "modulo": v.modulo.nombre if v.modulo else None,
            "fecha": str(v.fecha) if v.fecha else None,
            "hora": str(v.hora) if v.hora else None,
        }
        for v in ventas
    ]


@router.get("/ventas/resumen")
def resumen_ventas(db: Session = Depends(get_db)):
    total_ventas = db.query(func.sum(models.Venta.precio_unitario * models.Venta.cantidad)).scalar() or 0
    total_comisiones = db.query(func.sum(models.Venta.comision)).scalar() or 0
    return {
        "total_ventas": total_ventas,
        "total_comisiones": total_comisiones
    }
    
    
    

@router.put("/ventas/{venta_id}/cancelar", response_model=schemas.VentaResponse)
def cancelar_venta(
    venta_id: int,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    venta = db.query(models.Venta).filter_by(id=venta_id).first()
    if not venta:
        raise HTTPException(status_code=404, detail="Venta no encontrada")

    if venta.cancelada:
        raise HTTPException(status_code=400, detail="La venta ya fue cancelada")

    # Validar permisos
    if current_user.rol != models.RolEnum.admin:
        if current_user.rol != models.RolEnum.encargado or venta.modulo_id != current_user.modulo_id:
            raise HTTPException(status_code=403, detail="No tienes permisos para cancelar esta venta")

    # ── Venta espejo de un plan tarifario ────────────────────────────────────
    # crear_plan_tarifario crea la venta espejo con un producto que NO existe en
    # inventario_modulo ("PAGO INICIAL - X" / "X - PLAN SIN ENGANCHE"), asi que el
    # reintegro de mas abajo nunca encuentra la fila y el stock se pierde. Cuando
    # la venta pertenece a un plan, la reversion la hace revertir_plan() usando
    # plan.equipo, que si es el nombre real.
    plan = (
        db.query(models.PlanTarifario)
        .filter(models.PlanTarifario.venta_pi_id == venta.id)
        .first()
    )
    if plan is None and venta.tipo_venta == "plan" and venta.folio:
        # Pago dividido: venta_pi_id apunta solo a la primera parte. Si estan
        # cancelando la segunda, se llega al plan por el folio compartido.
        ids_folio = [
            row.id
            for row in db.query(models.Venta.id).filter(
                models.Venta.folio == venta.folio,
                models.Venta.tipo_venta == "plan",
            ).all()
        ]
        if ids_folio:
            plan = (
                db.query(models.PlanTarifario)
                .filter(models.PlanTarifario.venta_pi_id.in_(ids_folio))
                .first()
            )

    if plan is not None:
        # Devuelve inventario con plan.equipo, escribe el kardex correcto, libera
        # el IMEI, resta del CorteDia y borra el plan. Las ventas espejo quedan
        # con cancelada=True (no se borran) para no romper el historial ni el
        # response_model de este endpoint. Se sale antes del reintegro y del
        # registrar_kardex de abajo: un solo movimiento por reversion.
        revertir_plan(db, plan, current_user, borrar_ventas_espejo=False)

        # Explicito, no de rebote: revertir_plan marca las ventas espejo, pero esta
        # cancelacion no puede depender de que la venta actual caiga en esa lista.
        venta.cancelada = True

        db.commit()
        db.refresh(venta)
        return venta

    # Reintegrar inventario — lookup por clave para evitar duplicados por variación de nombre
    prod_general = (
        db.query(models.InventarioGeneral)
        .filter(models.InventarioGeneral.producto == venta.producto)
        .first()
    )
    inventario = None
    if prod_general:
        inventario = (
            db.query(models.InventarioModulo)
            .filter(
                models.InventarioModulo.clave     == prod_general.clave,
                models.InventarioModulo.modulo_id == venta.modulo_id
            )
            .first()
        )
    if not inventario:
        # Fallback: intentar por nombre exacto
        inventario = (
            db.query(models.InventarioModulo)
            .filter(
                models.InventarioModulo.producto  == venta.producto,
                models.InventarioModulo.modulo_id == venta.modulo_id
            )
            .first()
        )

    if inventario:
        inventario.cantidad += venta.cantidad
    else:
        print(
            f"[ALERTA cancelación] Venta {venta.id} (producto '{venta.producto}', "
            f"módulo {venta.modulo_id}) no tiene fila en inventario_modulo. "
            f"Stock NO devuelto, revisar manualmente."
        )

    # Revertir equipo Telcel a surtido (solo si la venta trae IMEI)
    if venta.tipo_producto == "telefono" and venta.imei:
        imei_limpio = str(venta.imei).strip()
        equipo = (
            db.query(models.EquiposTelcel)
            .filter(models.EquiposTelcel.imei == imei_limpio)
            .first()
        )
        if equipo and mismo_folio(equipo.folio_venta, venta.folio):
            equipo.estatus = "surtido"
            equipo.fecha_venta = None
            equipo.folio_venta = None
        else:
            print(
                f"[ALERTA cancelacion IMEI] Venta {venta.id} folio {venta.folio} "
                f"imei '{venta.imei}': equipo no encontrado o folio_venta no coincide. "
                f"IMEI NO revertido, revisar manualmente."
            )

    # Marcar cancelada
    venta.cancelada = True

    # 🔥 Registrar Kardex (ENTRADA por cancelación)
    registrar_kardex(
        db=db,
        producto=venta.producto,
        tipo_producto=venta.tipo_producto,
        cantidad=venta.cantidad,
        tipo_movimiento="CANCELACION_VENTA",
        usuario_id=current_user.id,
        modulo_origen_id=None,
        modulo_destino_id=venta.modulo_id,
        referencia_id=venta.id
    )

    db.commit()
    db.refresh(venta)

    return venta


@router.put("/ventas/{venta_id}/devolver", response_model=schemas.VentaResponse)
def devolver_venta(
    venta_id: int,
    data: schemas.DevolucionCreate,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    venta = db.query(models.Venta).filter_by(id=venta_id).first()
    if not venta:
        raise HTTPException(status_code=404, detail="Venta no encontrada")

    if current_user.rol not in (models.RolEnum.admin, models.RolEnum.encargado):
        raise HTTPException(status_code=403, detail="Solo encargado o admin pueden registrar devoluciones")
    if current_user.rol == models.RolEnum.encargado and venta.modulo_id != current_user.modulo_id:
        raise HTTPException(status_code=403, detail="No puedes devolver ventas de otro modulo")

    if venta.cancelada:
        raise HTTPException(status_code=400, detail="Esta venta esta cancelada, no se puede devolver")
    if venta.devuelta:
        raise HTTPException(status_code=400, detail="Esta venta ya fue devuelta")

    hoy_limite = datetime.now(zona_horaria).date()
    if venta.fecha and (hoy_limite - venta.fecha).days > 30:
        raise HTTPException(
            status_code=400,
            detail="No se pueden devolver ventas con mas de 30 dias de antiguedad"
        )

    cobrado = (venta.precio_unitario or 0) * (venta.cantidad or 0)
    if data.monto is None or data.monto <= 0:
        raise HTTPException(status_code=400, detail="El monto debe ser mayor a 0")
    if data.monto > cobrado + 0.01:
        raise HTTPException(status_code=400, detail=f"El monto no puede ser mayor a lo cobrado (${cobrado:.2f})")

    hoy = datetime.now(zona_horaria).date()

    # Reintegrar inventario
    prod_general = (
        db.query(models.InventarioGeneral)
        .filter(models.InventarioGeneral.producto == venta.producto)
        .first()
    )
    inventario = None
    if prod_general:
        inventario = (
            db.query(models.InventarioModulo)
            .filter(
                models.InventarioModulo.clave == prod_general.clave,
                models.InventarioModulo.modulo_id == venta.modulo_id
            )
            .first()
        )
    if not inventario:
        inventario = (
            db.query(models.InventarioModulo)
            .filter(
                models.InventarioModulo.producto == venta.producto,
                models.InventarioModulo.modulo_id == venta.modulo_id
            )
            .first()
        )

    if inventario:
        inventario.cantidad += venta.cantidad
    else:
        print(
            f"[ALERTA devolucion] Venta {venta.id} producto '{venta.producto}' "
            f"modulo {venta.modulo_id}: sin fila en inventario_modulo. Stock NO devuelto."
        )

    # Revertir IMEI si es telefono
    if venta.tipo_producto == "telefono" and venta.imei:
        imei_limpio = str(venta.imei).strip()
        equipo = (
            db.query(models.EquiposTelcel)
            .filter(models.EquiposTelcel.imei == imei_limpio)
            .first()
        )
        if equipo and mismo_folio(equipo.folio_venta, venta.folio):
            equipo.estatus = "surtido"
            equipo.fecha_venta = None
            equipo.folio_venta = None
        else:
            print(
                f"[ALERTA devolucion IMEI] Venta {venta.id} folio {venta.folio} "
                f"imei '{venta.imei}': no revertido, revisar manualmente."
            )

    # Marcar la venta
    venta.devuelta = True
    venta.fecha_devolucion = hoy
    venta.monto_devuelto = data.monto
    venta.devuelta_por = current_user.id
    venta.comision_monto = 0

    # Registrar en tabla devoluciones
    dev = models.Devolucion(
        venta_id=venta.id,
        folio=venta.folio,
        modulo_id=venta.modulo_id,
        fecha=hoy,
        monto=data.monto,
        motivo=data.motivo,
        usuario_id=current_user.id,
        creado_at=datetime.now(zona_horaria).replace(tzinfo=None),
    )
    db.add(dev)

    registrar_kardex(
        db=db,
        producto=venta.producto,
        tipo_producto=venta.tipo_producto,
        cantidad=venta.cantidad,
        tipo_movimiento="DEVOLUCION",
        usuario_id=current_user.id,
        modulo_origen_id=None,
        modulo_destino_id=venta.modulo_id,
        referencia_id=venta.id
    )

    db.commit()
    db.refresh(venta)

    return venta








# from fastapi import APIRouter, Depends, HTTPException
# from sqlalchemy.orm import Session
# from sqlalchemy import func
# from typing import List
# from datetime import datetime
# from io import BytesIO
# from reportlab.pdfgen import canvas
# from reportlab.lib.pagesizes import letter
# import requests
# import uuid
# from supabase import create_client

# from .. import models, schemas
# from ..database import get_db
# from ..dependencies import get_current_user
# from ..config import zona_horaria  # si ya la tienes en tu config

# router = APIRouter()

# # --- Configuración ---
# SUPABASE_URL = "https://TU_PROYECTO.supabase.co"
# SUPABASE_KEY = "TU_SUPABASE_KEY"
# WHATSAPP_TOKEN = "TU_TOKEN_PERMANENTE"
# PHONE_NUMBER_ID = "861665657026345"  # tu ID de app Meta
# BUCKET_NAME = "tickets"


# # --- Función: Generar ticket PDF ---
# def generar_ticket_pdf(cliente: str, telefono: str, ventas: List[models.Venta]):
#     buffer = BytesIO()
#     pdf = canvas.Canvas(buffer, pagesize=letter)

#     pdf.setFont("Helvetica-Bold", 16)
#     pdf.drawString(200, 750, "Ticket de Compra")

#     pdf.setFont("Helvetica", 12)
#     pdf.drawString(50, 720, f"Cliente: {cliente}")
#     pdf.drawString(50, 700, f"Teléfono: {telefono}")
#     pdf.drawString(50, 680, f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}")

#     pdf.drawString(50, 650, "Productos:")
#     y = 630
#     total = 0
#     for v in ventas:
#         linea = f"- {v.producto} x{v.cantidad}  ${v.precio_unitario:.2f} c/u"
#         pdf.drawString(70, y, linea)
#         y -= 20
#         total += v.cantidad * v.precio_unitario

#     pdf.setFont("Helvetica-Bold", 12)
#     pdf.drawString(50, y - 10, f"TOTAL: ${total:.2f}")

#     pdf.showPage()
#     pdf.save()
#     buffer.seek(0)
#     return buffer


# # --- Subir PDF a Supabase ---
# def subir_ticket_supabase(buffer):
#     supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
#     nombre_archivo = f"ticket_{uuid.uuid4()}.pdf"
#     ruta = f"{BUCKET_NAME}/{nombre_archivo}"
#     supabase.storage.from_(BUCKET_NAME).upload(ruta, buffer.getvalue(), {"content-type": "application/pdf"})
#     public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(ruta)
#     return public_url


# # --- Enviar ticket por WhatsApp ---
# def enviar_ticket_whatsapp(numero_cliente: str, url_pdf: str):
#     url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages"
#     payload = {
#         "messaging_product": "whatsapp",
#         "to": numero_cliente,
#         "type": "document",
#         "document": {
#             "link": url_pdf,
#             "filename": "ticket.pdf",
#             "caption": "Gracias por tu compra 💙 Aquí está tu ticket."
#         }
#     }
#     headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
#     response = requests.post(url, json=payload, headers=headers)
#     return response.json()





@router.post("/ventas/multiples", response_model=List[schemas.VentaResponse])
def crear_ventas_multiples(
    venta: schemas.VentaMultipleCreate,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):
    ventas_realizadas = []

    # calcular la fecha/hora una sola vez (evita inconsistencias por zona horaria)
    fecha_actual = datetime.now(zona_horaria)

    # Si el request trae un folio (ej. segunda llamada de pago dividido), reusarlo.
    # Si no, generar uno nuevo (excepto Cadenas id=7).
    if venta.folio:
        folio_venta = venta.folio
    elif current_user.modulo.id != 7:
        seq = db.execute(text("SELECT nextval('venta_folio_seq')")).scalar()
        folio_venta = f"V-{seq}"
    else:
        folio_venta = None

    for item in venta.productos:
        com = (
            db.query(models.Comision)
            .filter(func.lower(models.Comision.producto) == item.producto.strip().lower())
            .filter(models.Comision.activo == True)
            .first()
        )
        sin_comision   = item.skip_comision or (item.tipo_producto != 'telefono' and item.precio_unitario < 30)
        comision_id    = None if sin_comision else (com.id if com else None)
        comision_monto = None if sin_comision else (com.cantidad if com else None)
        modulo_id = current_user.modulo.id
        verificar_modulo_no_congelado(db, modulo_id)

        inventario = (
            db.query(models.InventarioModulo)
            .filter(
                models.InventarioModulo.modulo_id == modulo_id,
                models.InventarioModulo.producto == item.producto
            )
            .first()
        )

        if not inventario:
            raise HTTPException(status_code=400, detail=f"No hay inventario para el producto: {item.producto}")

        if inventario.cantidad < item.cantidad:
            raise HTTPException(status_code=400, detail=f"Inventario insuficiente para el producto: {item.producto}")

        # disminuir inventario
        inventario.cantidad -= item.cantidad

        # detectar tipo de producto buscando la palabra TELEFONO en cualquier parte
        tipo_producto = (
            "telefono"
            if "TELEFONO" in item.producto.strip().upper()
            else "accesorios"
        )

        nueva = models.Venta(
            empleado_id=current_user.id,
            modulo_id=modulo_id,
            producto=item.producto,
            cantidad=item.cantidad,
            precio_unitario=item.precio_unitario,
            total=item.cantidad * item.precio_unitario,
            metodo_pago=venta.metodo_pago,
            comision_id=comision_id,
            comision_monto=comision_monto,
            tipo_producto=tipo_producto,
            fecha=fecha_actual.date(),
            hora=fecha_actual.time(),
            telefono_cliente=venta.telefono_cliente,
            folio=folio_venta,
            imei=item.imei,
            clasificacion=item.clasificacion,
        )

        db.add(nueva)
        db.flush()

        registrar_kardex(
            db=db,
            producto=nueva.producto,
            tipo_producto=nueva.tipo_producto,
            cantidad=nueva.cantidad,
            tipo_movimiento="VENTA",
            usuario_id=current_user.id,
            modulo_origen_id=modulo_id,
            referencia_id=nueva.id
        )

        ventas_realizadas.append(nueva)

    # confirmar transacción de ventas e inventario
    db.commit()

    # refrescar objetos creados para tener los id y relaciones
    for v in ventas_realizadas:
        db.refresh(v)

    # --- UPSET/ACTUALIZACIÓN de CorteDia ---
    try:
        # obtener o crear corte para la fecha y módulo
        fecha_corte = fecha_actual.date()
        modulo_id = current_user.modulo.id

        corte = db.query(models.CorteDia).filter(
            models.CorteDia.fecha == fecha_corte,
            models.CorteDia.modulo_id == modulo_id
        ).first()

        if not corte:
            corte = models.CorteDia(
                fecha=fecha_corte,
                modulo_id=modulo_id,
                total_efectivo=0.0,
                total_tarjeta=0.0,
                adicional_recargas=0.0,
                adicional_transporte=0.0,
                adicional_otros=0.0,
                total_sistema=0.0,
                total_general=0.0,
                accesorios_efectivo=0.0,
                accesorios_tarjeta=0.0,
                accesorios_total=0.0,
                telefonos_efectivo=0.0,
                telefonos_tarjeta=0.0,
                telefonos_total=0.0
            )
            db.add(corte)
            db.flush()  # asegura que corte tenga id si es necesario

        # Sumarizar las ventas realizadas en este request al corte
        suma_request = 0.0
        for v in ventas_realizadas:
            pago = (v.metodo_pago or "").strip().lower()
            es_efectivo = pago == "efectivo" or pago == "cash"  # ajusta si tienes otros valores
            total_v = float(v.total or 0)

            if es_efectivo:
                corte.total_efectivo = (corte.total_efectivo or 0) + total_v
                if v.tipo_producto == "accesorios":
                    corte.accesorios_efectivo = (corte.accesorios_efectivo or 0) + total_v
                else:
                    corte.telefonos_efectivo = (corte.telefonos_efectivo or 0) + total_v
            else:
                # todo lo que no sea "efectivo" lo acumulamos en tarjeta (ajusta según necesites)
                corte.total_tarjeta = (corte.total_tarjeta or 0) + total_v
                if v.tipo_producto == "accesorios":
                    corte.accesorios_tarjeta = (corte.accesorios_tarjeta or 0) + total_v
                else:
                    corte.telefonos_tarjeta = (corte.telefonos_tarjeta or 0) + total_v

            # totales por tipo
            if v.tipo_producto == "accesorios":
                corte.accesorios_total = (corte.accesorios_total or 0) + total_v
            else:
                corte.telefonos_total = (corte.telefonos_total or 0) + total_v

            # acumulador para total sistema y general
            corte.total_sistema = (corte.total_sistema or 0) + total_v
            suma_request += total_v

        # Actualizar total_general (si quieres incluir ya las adicionales, agrégalas aquí)
        corte.total_general = (corte.total_general or 0) + suma_request

        db.commit()
        # opcional: db.refresh(corte)
    except Exception as e:
        # No queremos romper la respuesta si falla la actualización del corte,
        # pero sí devolvemos información del error en logs.
        db.rollback()
        # loguear e.g. logger.error(...) si tienes logger
        print("Error actualizando CorteDia:", e)

    # Generar ticket PDF y enviar por WhatsApp
    try:
        pdf_buffer = generar_ticket_pdf(
            cliente=venta.cliente if hasattr(venta, "cliente") else "Cliente",
            telefono=venta.telefono_cliente,
            ventas=ventas_realizadas,
        )
        url_pdf = subir_ticket_supabase(pdf_buffer)
        respuesta_whatsapp = enviar_ticket_whatsapp(venta.telefono_cliente, url_pdf)
    except Exception as e:
        respuesta_whatsapp = {"error": str(e)}

    # Conversión manual segura y respuesta
    return [
        schemas.VentaResponse(
            id=v.id,
            empleado=schemas.UsuarioResponse.from_orm(v.empleado) if v.empleado else None,
            modulo=v.modulo,
            producto=v.producto,
            cantidad=v.cantidad,
            precio_unitario=v.precio_unitario,
            metodo_pago=v.metodo_pago,
            total=v.precio_unitario * v.cantidad,
            comision=(v.comision_monto if v.comision_monto is not None
                      else (db.query(models.Comision).filter_by(id=v.comision_id).first().cantidad
                            if v.comision_id else None)),
            fecha=v.fecha,
            hora=v.hora,
            cancelada=v.cancelada,
            folio=v.folio,
        )
        for v in ventas_realizadas
    ]



@router.post("/venta_chips", response_model=schemas.VentaChipResponse)
def crear_venta_chip(
    venta: schemas.VentaChipCreate,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):
    
    if venta.cvip is None:
        raise HTTPException(
            status_code=400,
            detail="Debes seleccionar una opcion (Si o No)"
        )

    # Bloquear número duplicado (un número = un solo registro, salvo que el anterior esté cancelado)
    existente = db.query(models.VentaChip).filter(
        models.VentaChip.numero_telefono == venta.numero_telefono,
        models.VentaChip.cancelada == False
    ).first()
    if existente:
        raise HTTPException(
            status_code=400,
            detail=f"El número {venta.numero_telefono} ya tiene un chip registrado"
        )

    verificar_modulo_no_congelado(db, current_user.modulo_id)
    fecha_actual = datetime.now(zona_horaria)

    # El asesor captura solo el dia, sin hora: se guarda a las 00:00. Si viene
    # la fecha, el tramite ya se hizo y la venta nace marcada como realizada.
    fecha_porta = None
    realizada = False
    if venta.fecha_portabilidad:
        fecha_porta = datetime.combine(venta.fecha_portabilidad, time(0, 0))
        realizada = True

    nueva_venta = models.VentaChip(
        empleado_id=current_user.id,
        tipo_chip=venta.tipo_chip,
        numero_telefono=venta.numero_telefono,
        monto_recarga=venta.monto_recarga,
        cvip=venta.cvip,
        fecha=fecha_actual.date(),
        hora=fecha_actual.time(),
        imei=venta.imei,
        iccid=venta.iccid,
        curp=venta.curp,
        nip=venta.nip,
        fecha_portabilidad=fecha_porta,
        portabilidad_realizada=realizada,
        cambio_chip=venta.cambio_chip,
    )

    # ── Auto-validar Tarjetas PayJoy (comisión fija, sin necesidad de validación admin)
    if nueva_venta.tipo_chip == "Tarjetas PayJoy":
        nueva_venta.comision = 50.00
        nueva_venta.validado = True
        nueva_venta.comision_pagada = True

    db.add(nueva_venta)
    db.commit()
    db.refresh(nueva_venta)
    return nueva_venta



@router.get("/venta_chips/verificar_numero/{numero}")
def verificar_numero_duplicado(numero: str, db: Session = Depends(get_db)):
    existe = db.query(models.VentaChip).filter(
        models.VentaChip.numero_telefono == numero
    ).first()
    return {"duplicado": existe is not None}


@router.get("/venta_chips/pendientes", response_model=list[schemas.VentaChipResponse])
def obtener_chips_pendientes(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):
    return (
        db.query(models.VentaChip)
        .filter(
            models.VentaChip.validado == False,
            models.VentaChip.cancelada == False,
        )
        .all()
    )


@router.post("/venta_chips/pagar_comisiones", response_model=schemas.PagarComisionesResponse)
def pagar_comisiones(
    data: schemas.PagarComisionesInput,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):
    try:
        no_encontrados: list[str] = []
        normales_pagados = 0
        incubadora_validados = 0
        total_pagado_normales = 0.0
        total_pendiente_incubadora = 0.0
        detalle_incubadora: list[dict] = []

        for chip_id in data.chip_ids:
            chip = db.query(models.VentaChip).filter(
                models.VentaChip.id == chip_id
            ).first()

            if chip is None:
                no_encontrados.append(str(chip_id))
                continue
            if chip.cancelada:
                no_encontrados.append(chip.numero_telefono)
                continue
            if chip.comision_pagada:
                no_encontrados.append(chip.numero_telefono)
                continue

            numero_limpio = chip.numero_telefono.strip().split()[0]
            row = db.execute(
                text("SELECT comision_telcel FROM comisiones_telcel WHERE numero = :num LIMIT 1"),
                {"num": numero_limpio}
            ).fetchone()
            comision_nueva = float(row[0]) if row else (chip.comision or 0)
            chip.comision = comision_nueva

            if chip.es_incubadora:
                chip.validado = True
                chip.descripcion_rechazo = None
                incubadora_validados += 1
                total_pendiente_incubadora += comision_nueva
                detalle_incubadora.append({
                    "numero": numero_limpio,
                    "empleado": chip.empleado.username if chip.empleado else "—",
                    "comision": comision_nueva,
                })
            else:
                chip.validado = True
                chip.comision_pagada = True
                normales_pagados += 1
                total_pagado_normales += comision_nueva

        db.commit()
        return {
            "chips_normales_pagados": normales_pagados,
            "chips_incubadora_validados": incubadora_validados,
            "chips_no_encontrados": len(no_encontrados),
            "total_pagado_normales": round(total_pagado_normales, 2),
            "total_pendiente_incubadora": round(total_pendiente_incubadora, 2),
            "detalle_incubadora": detalle_incubadora,
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error interno: {type(e).__name__}: {str(e)}")


@router.get("/venta_chips", response_model=list[schemas.VentaChipResponse])
def obtener_ventas_chips(
    empleado_id: Optional[int] = None,
    modulo_nombre: Optional[str] = None,
    solo_pendientes: bool = False,
    solo_incubadora: bool = False,
    fecha_inicio: Optional[date] = None,
    fecha_fin: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):
    print(f"[chips] usuario={current_user.username} rol={current_user.rol!r} modulo_id={current_user.modulo_id}")

    def _aplicar_filtros(query):
        query = query.options(
            selectinload(models.VentaChip.empleado)
            .selectinload(models.Usuario.modulo)
        )
        if solo_pendientes:
            query = query.filter(
                models.VentaChip.validado.isnot(True),
                models.VentaChip.cancelada.isnot(True),
            )
        if solo_incubadora:
            query = query.filter(
                models.VentaChip.es_incubadora == True,
                models.VentaChip.validado.isnot(True),
            )
        if fecha_inicio:
            query = query.filter(models.VentaChip.fecha >= fecha_inicio)
        if fecha_fin:
            query = query.filter(models.VentaChip.fecha <= fecha_fin)
        return query.order_by(models.VentaChip.fecha.desc())

    if current_user.is_admin:
        query = db.query(models.VentaChip)
        if empleado_id is not None:
            query = query.filter(models.VentaChip.empleado_id == empleado_id)
        resultado = _aplicar_filtros(query).all()
        print(f"[chips] admin -> {len(resultado)} chips")
        return resultado
    elif str(current_user.rol).replace("RolEnum.", "") == "encargado" or current_user.rol == "encargado":
        encargado_modulo = db.query(models.Modulo).filter(models.Modulo.id == current_user.modulo_id).first()
        if not encargado_modulo:
            print(f"[chips] encargado sin modulo -> devolviendo []")
            return []
        nombre_modulo = encargado_modulo.nombre
        print(f"[chips] encargado modulo_nombre={nombre_modulo!r}")
        empleados_modulo = (
            db.query(models.Usuario.id)
            .join(models.Modulo, models.Usuario.modulo_id == models.Modulo.id)
            .filter(models.Modulo.nombre == nombre_modulo)
            .all()
        )
        ids_empleados = [e.id for e in empleados_modulo]
        print(f"[chips] empleados en modulo {nombre_modulo!r}: {ids_empleados}")
        query = (
            db.query(models.VentaChip)
            .filter(models.VentaChip.empleado_id.in_(ids_empleados))
        )
        resultado = _aplicar_filtros(query).all()
        print(f"[chips] encargado -> {len(resultado)} chips")
        return resultado
    else:
        query = db.query(models.VentaChip).filter(models.VentaChip.empleado_id == current_user.id)
        resultado = _aplicar_filtros(query).all()
        print(f"[chips] asesor -> {len(resultado)} chips")
        return resultado


@router.put("/venta_chips/{id}/validar", response_model=schemas.VentaChipResponse)
def validar_chip(
    id: int,
    data: schemas.ComisionInput = Body(...),
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):
    chip = db.query(models.VentaChip).filter(models.VentaChip.id == id).first()
    if not chip:
        raise HTTPException(status_code=404, detail="Venta de chip no encontrada")

    if chip.validado:
        raise HTTPException(status_code=400, detail="Ya ha sido validado")

    if chip.tipo_chip == "Tarjetas PayJoy":
        chip.validado = True
        chip.comision_pagada = True
        chip.comision = 50.00
        chip.fecha_validacion = datetime.now(zona_horaria)
        chip.validado_por_id = current_user.id
        db.commit()
        db.refresh(chip)
        return chip

    tipo = chip.tipo_chip
    monto = int(chip.monto_recarga or 0)

    if tipo == "Activacion":
        if data.comision_manual is None:
            raise HTTPException(status_code=400, detail="Debe proporcionar una comisión para chip Activacion")
        chip.comision = data.comision_manual
    else:
        # Comisiones usando rangos (min, max)
        comisiones_por_chip = {
            "Chip Equipo": [
                ((0,50), 15),
                ((51, 100), 20),
                ((101, 1000), 30)
            ],
            "Chip Express": [
                ((0, 50), 5),
                ((51, 100), 10),
                ((101, 150),30)
            ],
            "Portabilidad": [
                ((0, 500), 50),
                
            ],
            "Chip Cero/Libre": [
                ((0, 50), 25),
                ((51, 100), 30),
                ((101, 150),35)
                
            ],
            "Chip Preactivado": [
                ((0, 500), 35),
                
            ],

              "Chip Coppel": [
              ((0, 50), 10),
                ((51, 100), 15),
            ],

            "Portabilidad Coppel": [
                ((0, 500), 25),
            ],
            
            "Porta Otras cadenas": [
                ((0, 500), 50),
            ],

            "Tarjetas PayJoy": [
                ((0, 9999), 50),
            ],

            "Boletin 63": [
                ((0, 9999), 50),
            ],

        }

        if tipo not in comisiones_por_chip:
            raise HTTPException(status_code=404, detail="No hay comisión configurada para este tipo de chip")

        comision_asignada = None
        for (min_monto, max_monto), comision in comisiones_por_chip[tipo]:
            if min_monto <= monto <= max_monto:
                comision_asignada = comision
                break

        if comision_asignada is None:
            raise HTTPException(status_code=404, detail="Monto de recarga fuera de rango para este tipo de chip")

        chip.comision = comision_asignada

    chip.validado = True
    chip.fecha_validacion = datetime.now(zona_horaria)
    chip.validado_por_id = current_user.id

    db.commit()
    db.refresh(chip)

    return chip



@router.put("/venta_chips/{venta_id}/motivo_rechazo")
def motivo_rechazo_chip(
    venta_id: int,
    descripcion: str = Body(..., embed=True),
    db: Session = Depends(get_db),
):
    venta = db.query(models.VentaChip).filter_by(id=venta_id).first()
    if not venta:
        raise HTTPException(status_code=404, detail="Venta no encontrada")

    if descripcion.strip():
        venta.descripcion_rechazo = descripcion
        venta.es_incubadora = True
    else:
        venta.descripcion_rechazo = None
        venta.es_incubadora = False
    db.commit()
    return {"mensaje": "Motivo de rechazo registrado"}

@router.get("/chips_rechazados", response_model=List[schemas.VentaChipResponse])
def obtener_chips_rechazados(
    empleado_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):

    query = db.query(models.VentaChip).filter(
        models.VentaChip.descripcion_rechazo != None,
        models.VentaChip.validado == False
    )

    # 🔹 Si NO es admin → solo ve sus chips
    if current_user.rol != "admin":
        query = query.filter(
            models.VentaChip.empleado_id == current_user.id
        )

    # 🔹 Si es admin y selecciona empleado
    elif empleado_id:
        query = query.filter(
            models.VentaChip.empleado_id == empleado_id
        )

    return query.all()

@router.put("/validar_chip_incubadora/{chip_id}", response_model=schemas.VentaChipResponse)
def validar_chip_incubadora(
    chip_id: int,
    data: schemas.ValidarChipIncubadoraRequest,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):

    chip = db.query(models.VentaChip).filter(
        models.VentaChip.id == chip_id
    ).first()

    if not chip:
        raise HTTPException(status_code=404, detail="Chip no encontrado")

    if chip.validado:
        raise HTTPException(status_code=400, detail="El chip ya está validado")

    tipo = chip.tipo_chip
    monto = int(chip.monto_recarga or 0)

    comisiones_por_chip = {
        "Chip Equipo": [
            ((0,50), 15),
            ((51, 100), 20),
            ((101, 1000), 30)
        ],
        "Chip Express": [
            ((0, 50), 5),
            ((51, 100), 10),
            ((101, 150),30)
        ],
        "Portabilidad": [
            ((0, 500), 50),
        ],
        "Chip Cero/Libre": [
            ((0, 50), 25),
            ((51, 100), 30),
            ((101, 150),35)
        ],
        "Chip Preactivado": [
            ((0, 500), 35),
        ],
        "Chip Coppel": [
            ((0, 50), 10),
            ((51, 100), 15),
        ],
        "Portabilidad Coppel": [
            ((0, 500), 25),
        ],
        "Porta Otras cadenas": [
            ((0, 500), 50),
        ],

        "Tarjetas PayJoy": [
            ((0, 9999), 50),
        ],
    }

    # Comisión SIEMPRE manual en incubadora (todos los tipos)
    if not data.comision_manual or data.comision_manual <= 0:
        raise HTTPException(
            status_code=400,
            detail="La comisión manual es obligatoria"
        )

    chip.comision = data.comision_manual
    # 🔹 Marcar como incubadora
    

    # 🔹 Limpiar rechazo
    chip.descripcion_rechazo = None

    # 🔹 Validar
    chip.validado = True
    chip.fecha_validacion = datetime.now(zona_horaria)
    chip.validado_por_id = current_user.id

    db.commit()
    db.refresh(chip)

    return chip

@router.delete("/eliminar_chip/{chip_id}", status_code=status.HTTP_200_OK)
def eliminar_chip(chip_id: int, db: Session = Depends(get_db)):
    chip = db.query(models.VentaChip).filter(models.VentaChip.id == chip_id).first()
    if not chip:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chip no encontrado"
        )
    
    db.delete(chip)
    db.commit()
    return {"message": f"Chip con id {chip_id} eliminado correctamente"}






@router.put("/venta_telefonos/{venta_id}/cancelar")
def cancelar_venta_telefono(
    venta_id: int,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):
    venta = db.query(models.VentaTelefono).filter_by(id=venta_id).first()

    if not venta:
        raise HTTPException(status_code=404, detail="Venta no encontrada")

    if venta.cancelada:
        raise HTTPException(status_code=400, detail="La venta ya está cancelada")

    # Buscar el inventario del módulo del vendedor
    empleado = db.query(models.Usuario).filter_by(id=venta.empleado_id).first()
    if not empleado or not empleado.modulo_id:
        raise HTTPException(status_code=400, detail="El vendedor no tiene módulo asignado")

    inventario = db.query(models.InventarioTelefono).filter_by(
        marca=venta.marca,
        modelo=venta.modelo,
        modulo_id=empleado.modulo_id
    ).first()

    if not inventario:
        raise HTTPException(status_code=404, detail="Inventario de teléfono no encontrado")

    # Revertir cancelación
    inventario.cantidad += 1
    venta.cancelada = True

    db.commit()

    return {"mensaje": "Venta cancelada y stock restaurado"}



@router.get("/ventas_telefonos", response_model=List[schemas.VentaTelefonoResponse])
def obtener_ventas_telefonos(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):
    # Solo mostrar ventas del módulo del usuario (si aplica)
    ventas = (
        db.query(models.VentaTelefono)
        .filter(models.VentaTelefono.empleado.has(modulo_id=current_user.modulo_id))
        .order_by(models.VentaTelefono.fecha.desc(), models.VentaTelefono.hora.desc())
        .all()
    )
    return ventas



@router.get("/corte-general")
def corte_general(
    db: Session = Depends(get_db),
    modulo_id: int | None = Query(None),    
    current_user: models.Usuario = Depends(get_current_user)
):
    hoy = datetime.now(zona_horaria).date()

    modulo_final = modulo_id or current_user.modulo_id

    ventas = db.query(models.Venta).filter(
        func.date(models.Venta.fecha) == hoy,
        models.Venta.modulo_id == modulo_final
    ).all()

    ventas_productos = [v for v in ventas if v.tipo_producto == "accesorios"]
    ventas_telefonos = [v for v in ventas if v.tipo_producto == "telefono"]

    # ACCESORIOS
    efectivo_productos = sum(
        v.total for v in ventas_productos
        if (v.metodo_pago or "").lower() == "efectivo" and not v.cancelada
    )

    tarjeta_productos = sum(
        v.total for v in ventas_productos
        if (v.metodo_pago or "").lower() == "tarjeta" and not v.cancelada
    )

    def total_tel(v):
        return (v.precio_unitario or 0) * (v.cantidad or 1)

    efectivo_tel = sum(
        total_tel(v)
        for v in ventas_telefonos
        if (v.metodo_pago or "").lower() == "efectivo" and not v.cancelada
    )

    tarjeta_tel = sum(
        total_tel(v)
        for v in ventas_telefonos
        if (v.metodo_pago or "").lower() == "tarjeta" and not v.cancelada
    )

    total_efectivo = efectivo_productos + efectivo_tel
    total_tarjeta = tarjeta_productos + tarjeta_tel
    total_sistema = total_efectivo + total_tarjeta

    print("TOTAL VENTAS:", len(ventas))
    print("TELEFONOS:", len([v for v in ventas if v.tipo_producto == "telefono"]))
    print("TIPOS:", list(set([v.tipo_producto for v in ventas])))

    return {
    "ventas_telefonos": {
        "efectivo": round(efectivo_tel, 2),
        "tarjeta": round(tarjeta_tel, 2),
        "total": round(efectivo_tel + tarjeta_tel, 2)
    },
    "ventas_productos": {
        "efectivo": round(efectivo_productos, 2),
        "tarjeta": round(tarjeta_productos, 2),
        "total": round(efectivo_productos + tarjeta_productos, 2)
    },
    "total_sistema": round(total_sistema, 2),
    "totales": {
        "efectivo": round(total_efectivo, 2),
        "tarjeta": round(total_tarjeta, 2),
        "sistema": round(total_sistema, 2),
        "general": round(total_sistema, 2)
    }
}
    


@router.get("/ventas/cortes")
def obtener_cortes(
    fecha: Optional[date] = Query(None),
    modulo_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):


    
    # Query base
    query = db.query(models.CorteDia)

    if fecha:
        query = query.filter(models.CorteDia.fecha == fecha)

    if modulo_id:
        query = query.filter(models.CorteDia.modulo_id == modulo_id)

    # 🔹 ORDEN CORRECTO (sin joinedload mal usado)
    cortes = query.order_by(models.CorteDia.fecha.desc()).all()

    cortes_completos = []

    for corte in cortes:

        # 🔹 Obtener ventas del día por módulo
        ventas = db.query(models.Venta).filter(
            func.date(models.Venta.fecha) == corte.fecha,
            models.Venta.modulo_id == corte.modulo_id,
            models.Venta.cancelada == False
        ).all()

        cortes_completos.append({
            "fecha": corte.fecha,
            "modulo_id": corte.modulo_id,

            # Totales generales
            "total_efectivo": corte.total_efectivo,
            "total_tarjeta": corte.total_tarjeta,
            "total_sistema": corte.total_sistema,
            "total_general": corte.total_general,

            # Adicionales
            "adicional_recargas": corte.adicional_recargas,
            "adicional_transporte": corte.adicional_transporte,
            "adicional_otros": corte.adicional_otros,

            # Accesorios
            "accesorios_efectivo": corte.accesorios_efectivo,
            "accesorios_tarjeta": corte.accesorios_tarjeta,
            "accesorios_total": corte.accesorios_total,

            # Teléfonos
            "telefonos_efectivo": corte.telefonos_efectivo,
            "telefonos_tarjeta": corte.telefonos_tarjeta,
            "telefonos_total": corte.telefonos_total,

            # 🔹 Ventas detalladas
            "ventas": [
                {
                    "id": v.id,
                    "producto": v.producto,
                    "tipo_producto": v.tipo_producto,
                    "tipo_venta": v.tipo_venta,
                    "precio_unitario": v.precio_unitario,
                    "cantidad": v.cantidad,
                    "total": v.total,

                    # 🔹 Fecha con hora real (evita bug timezone)
                    "fecha": f"{v.fecha} {v.hora}" if v.hora else str(v.fecha),

                    # 🔹 Nombre del empleado
                    "empleado": {
                        "id": v.empleado.id if v.empleado else None,
                        "username": v.empleado.username if v.empleado else "Sin nombre"
                    }
                }
                for v in ventas
            ]
        })

    return cortes_completos

    
@router.post("/cortes")
def crear_corte(
    corte_data: schemas.CorteDiaCreate,
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.rol not in ("encargado", "asesor"):
        raise HTTPException(status_code=403, detail="Solo los encargados y asesores pueden hacer cortes")
    if user.rol == "asesor" and getattr(user.modulo, "nombre", None) == "Cadenas Comerciales":
        raise HTTPException(status_code=403, detail="Los asesores de Cadenas Comerciales no pueden hacer cortes")

    if not user.modulo_id:
        raise HTTPException(status_code=400, detail="El usuario no tiene un módulo asignado")

    nuevo_corte = models.CorteDia(
        fecha=datetime.now(zona_horaria).date(),
        modulo_id=user.modulo_id,
        # Accesorios
        accesorios_efectivo=corte_data.accesorios_efectivo,
        accesorios_tarjeta=corte_data.accesorios_tarjeta,
        accesorios_total=corte_data.accesorios_total,
        # Teléfonos
        telefonos_efectivo=corte_data.telefonos_efectivo,
        telefonos_tarjeta=corte_data.telefonos_tarjeta,
        telefonos_total=corte_data.telefonos_total,
        # Totales
        total_efectivo=corte_data.total_efectivo,
        total_tarjeta=corte_data.total_tarjeta,
        total_sistema=corte_data.total_sistema,
        total_general=corte_data.total_general,
        # Adicionales
        adicional_recargas=corte_data.adicional_recargas,
        adicional_transporte=corte_data.adicional_transporte,
        adicional_otros=corte_data.adicional_otros,
    )

    db.add(nuevo_corte)
    db.commit()
    db.refresh(nuevo_corte)
    return nuevo_corte


@router.get("/cortes/hoy", response_model=Optional[schemas.CorteDiaResponse])
def obtener_corte_hoy(
    fecha: Optional[date] = Query(default=None),
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    target = fecha if fecha is not None else datetime.now(zona_horaria).date()
    total_devoluciones = (
        db.query(func.coalesce(func.sum(models.Devolucion.monto), 0.0))
        .filter(
            models.Devolucion.modulo_id == user.modulo_id,
            models.Devolucion.fecha == target
        )
        .scalar()
    ) or 0.0
    print(f"[cortes/hoy] fecha_param={fecha!r} target={target!r} modulo_id={user.modulo_id}")
    resultado = db.query(models.CorteDia).filter(
        models.CorteDia.fecha == target,
        models.CorteDia.modulo_id == user.modulo_id
    ).first()
    print(f"[cortes/hoy] resultado_fecha={getattr(resultado, 'fecha', None)!r}")
    if resultado is None:
        cc = db.query(models.CajaChica).filter(
            models.CajaChica.modulo_id == user.modulo_id,
            models.CajaChica.fecha == target,
        ).first()
        caja_chica_monto = float(cc.monto) if cc else 0.0
        if caja_chica_monto == 0.0 and total_devoluciones == 0.0:
            return None
        return {
            "id": 0,
            "fecha": target,
            "modulo_id": user.modulo_id,
            "accesorios_efectivo": 0.0,
            "accesorios_tarjeta": 0.0,
            "accesorios_total": 0.0,
            "telefonos_efectivo": 0.0,
            "telefonos_tarjeta": 0.0,
            "telefonos_total": 0.0,
            "total_efectivo": 0.0,
            "total_tarjeta": 0.0,
            "total_sistema": 0.0,
            "total_general": 0.0,
            "adicional_recargas": 0.0,
            "adicional_transporte": 0.0,
            "adicional_otros": 0.0,
            "adicional_mayoreo": 0.0,
            "adicional_mayoreo_para": None,
            "salida_efectivo": 0.0,
            "nota_salida": None,
            "enviado": False,
            "revisado_direccion": False,
            "revisado_por": None,
            "revisado_at": None,
            "caja_chica": caja_chica_monto,
            "devoluciones": float(total_devoluciones),
        }
    cc = db.query(models.CajaChica).filter(
        models.CajaChica.modulo_id == resultado.modulo_id,
        models.CajaChica.fecha == resultado.fecha,
    ).first()
    data = schemas.CorteDiaResponse.model_validate(resultado).model_dump()
    data["caja_chica"] = float(cc.monto) if cc else 0.0
    data["devoluciones"] = float(total_devoluciones)
    return data


@router.patch("/cortes/hoy/recargas", response_model=schemas.CorteDiaResponse)
def guardar_recargas(
    data: schemas.RecargasUpdate,
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    hoy = datetime.now(zona_horaria).date()
    corte = db.query(models.CorteDia).filter(
        models.CorteDia.fecha == hoy,
        models.CorteDia.modulo_id == user.modulo_id
    ).first()
    if corte and corte.fecha < datetime.now(zona_horaria).date():
        raise HTTPException(status_code=400, detail="El corte de días anteriores ya está cerrado y no se puede modificar")
    if not corte:
        corte = models.CorteDia(fecha=hoy, modulo_id=user.modulo_id)
        db.add(corte)
    corte.adicional_recargas = data.adicional_recargas
    corte.adicional_transporte = data.adicional_transporte
    corte.adicional_otros = data.adicional_otros
    corte.adicional_mayoreo = data.adicional_mayoreo
    corte.adicional_mayoreo_para = data.adicional_mayoreo_para
    db.commit()
    db.refresh(corte)
    cc = db.query(models.CajaChica).filter(
        models.CajaChica.modulo_id == corte.modulo_id,
        models.CajaChica.fecha == corte.fecha,
    ).first()
    result = schemas.CorteDiaResponse.model_validate(corte).model_dump()
    result["caja_chica"] = float(cc.monto) if cc else 0.0
    return result


@router.patch("/cortes/hoy/salida", response_model=schemas.CorteDiaResponse)
def guardar_salida(
    data: schemas.SalidaUpdate,
    user: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    hoy = datetime.now(zona_horaria).date()
    corte = db.query(models.CorteDia).filter(
        models.CorteDia.fecha == hoy,
        models.CorteDia.modulo_id == user.modulo_id
    ).first()
    if corte and corte.fecha < datetime.now(zona_horaria).date():
        raise HTTPException(status_code=400, detail="El corte de días anteriores ya está cerrado y no se puede modificar")
    if not corte:
        corte = models.CorteDia(fecha=hoy, modulo_id=user.modulo_id)
        db.add(corte)
    corte.salida_efectivo = data.salida_efectivo
    corte.nota_salida = data.nota_salida
    db.commit()
    db.refresh(corte)
    cc = db.query(models.CajaChica).filter(
        models.CajaChica.modulo_id == corte.modulo_id,
        models.CajaChica.fecha == corte.fecha,
    ).first()
    result = schemas.CorteDiaResponse.model_validate(corte).model_dump()
    result["caja_chica"] = float(cc.monto) if cc else 0.0
    total_dev = (
        db.query(func.coalesce(func.sum(models.Devolucion.monto), 0.0))
        .filter(
            models.Devolucion.modulo_id == corte.modulo_id,
            models.Devolucion.fecha == corte.fecha
        )
        .scalar()
    ) or 0.0
    result["devoluciones"] = float(total_dev)
    return result


@router.post("/cortes/hoy/enviar")
def enviar_corte_hoy():
    raise HTTPException(
        status_code=410,
        detail="Los cortes ahora se cierran automáticamente a medianoche. Ya no es necesario enviarlos.",
    )


# vamos a modificar ya no se que es 




@router.get("/comisiones/ciclo/{empleado_id}", response_model=schemas.ComisionesCicloResponse)
def obtener_comisiones_ciclo_admin(
    empleado_id: int,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(verificar_rol_requerido(models.RolEnum.admin))
):
    hoy = datetime.now(zona_horaria).date()
    dias_desde_lunes = hoy.weekday()
    inicio_ciclo = hoy - timedelta(days=dias_desde_lunes)
    fin_ciclo = inicio_ciclo + timedelta(days=6)
    fecha_pago = fin_ciclo + timedelta(days=3)

  
    ventas_chips = db.query(models.VentaChip).filter(
        models.VentaChip.empleado_id == empleado_id,
        models.VentaChip.validado == True,
        models.VentaChip.fecha >= inicio_ciclo,
        models.VentaChip.fecha <= fin_ciclo,
    ).all()

    ventas_accesorios = db.query(models.Venta).filter(
        models.Venta.empleado_id == empleado_id,
        models.Venta.fecha >= inicio_ciclo,
        models.Venta.fecha<= fin_ciclo,
        models.Venta.cancelada == False,
        models.Venta.tipo_producto == "accesorios"
    ).all()

    # ← CORRECCIÓN: usar models.Venta aquí para teléfonos (consistente)
    ventas_telefonos = db.query(models.Venta).filter(
        models.Venta.empleado_id == empleado_id,
        models.Venta.fecha >= inicio_ciclo,
        models.Venta.fecha <= fin_ciclo,
        models.Venta.cancelada == False,
        models.Venta.tipo_producto == "telefono"
    ).all()

    # ------------------------------------------------
    # Procesar ACCESORIOS: si comision_obj es None -> comision 0
    # ------------------------------------------------
    accesorios = []
    for v in ventas_accesorios:
        comision_unitaria = getattr(getattr(v, "comision_obj", None), "cantidad", 0)
        comision_total_attr = getattr(v, "comision_total", None)
        # En accesorios si no existe comision_total lo calculamos sólo si hay comision_obj
        comision_total = comision_total_attr if comision_total_attr is not None else (comision_unitaria * getattr(v, "cantidad", 0))
        # incluir solo si hay comisión (según tu regla)
        if comision_unitaria > 0 or (comision_total and comision_total > 0):
            accesorios.append({
                "producto": getattr(v, "producto", None),
                "cantidad": getattr(v, "cantidad", 0),
                "comision": comision_unitaria,
                "comision_total": comision_total,
                "tipo_venta": getattr(v, "tipo_venta", None),
                "fecha": getattr(v, "fecha", None),
                "hora": getattr(v, "hora", None)
            })

    # ------------------------------------------------
    # Procesar TELÉFONOS: siempre considerar comision_total cuando exista
    # ------------------------------------------------
    telefonos = []
    for v in ventas_telefonos:
        comision_unitaria = getattr(getattr(v, "comision_obj", None), "cantidad", 0)
        comision_total_attr = getattr(v, "comision_total", None)

        # En teléfonos: si comision_total existe lo usamos; si no, lo calculamos si comision_obj existe; si no, 0
        if comision_total_attr is not None:
            comision_total = comision_total_attr
        else:
            comision_total = (comision_unitaria * getattr(v, "cantidad", 0)) if comision_unitaria > 0 else 0

        telefonos.append({
            "producto": getattr(v, "producto", None),
            "cantidad": getattr(v, "cantidad", 0),
            "tipo_venta": getattr(v, "tipo_venta", None),
            "comision": comision_unitaria,
            "comision_total": comision_total,
            "fecha": getattr(v, "fecha", None),
            "hora": getattr(v, "hora", None)
        })

    # ------------------------------------------------
    # Procesar CHIPS
    # ------------------------------------------------
    chips = []
    for v in ventas_chips:
        com = getattr(v, "comision", 0) or 0
        if com > 0:
            chips.append({
                "tipo_chip": getattr(v, "tipo_chip", None),
                "numero_telefono": getattr(v, "numero_telefono", None),
                "comision": com,
                "fecha": getattr(v, "fecha", None),
                "hora": getattr(v, "hora", None)
            })

    # ------------------------------------------------
    # Totales (usar comision_total para accesorios y telefonos)
    # ------------------------------------------------
    total_accesorios = sum(v.get("comision_total", 0) or 0 for v in accesorios)
    total_telefonos = sum(v.get("comision_total", 0) or 0 for v in telefonos)
    total_chips = sum(v.get("comision", 0) or 0 for v in chips)

    return {
        "inicio_ciclo": inicio_ciclo,
        "fin_ciclo": fin_ciclo,
        "fecha_pago": fecha_pago,
        "total_chips": total_chips,
        "total_accesorios": total_accesorios,
        "total_telefonos": total_telefonos,
        "total_general": total_chips + total_accesorios + total_telefonos,
        "ventas_accesorios": accesorios,
        "ventas_telefonos": telefonos,
        "ventas_chips": chips
    }


@router.put("/ventas/{id}/comision_tipo", response_model=schemas.VentaTelefonoConComision)
def agregar_comision_por_tipo_venta(
    id: int,
    db: Session = Depends(get_db)
):
    venta = db.query(models.Venta).filter(models.Venta.id == id).first()
    if not venta:
        raise HTTPException(status_code=404, detail="Venta no encontrada")

    # Tabla de comisiones adicionales según tipo de venta
    comisiones_por_tipo = {
        "Contado": 10,
        "Paguitos": 110,
        "Pajoy": 100
    }

    # Comisión base (si no tiene, se asume 0)
    comision_base = venta.comision_obj.cantidad if venta.comision_obj else 0

    # Comisión extra según el tipo de venta
    comision_extra = comisiones_por_tipo.get(venta.tipo_venta, 0)

    # Cálculo total
    if venta.tipo_producto and venta.tipo_producto.lower() == "telefono":
        comision_total = (comision_base * venta.cantidad) + comision_extra
    else:
        comision_total = comision_base * venta.cantidad

    # Guardar cambios (si quisieras persistir la comisión total en BD, aquí podrías hacerlo)
    db.commit()
    db.refresh(venta)

    return {
        "id": venta.id,
        "producto": venta.producto,
        "cantidad": venta.cantidad,
        "tipo_venta": venta.tipo_venta,
        "tipo_producto": venta.tipo_producto,
        "comision_base": comision_base,
        "comision_extra": comision_extra,
        "comision_total": comision_total,
        "fecha": venta.fecha,
        "hora": venta.hora
    }




@router.get("/clientes_vip")
def clientes_vip(db: Session = Depends(get_db)):

    clientes = db.query(models.VentaChip).filter(
        models.VentaChip.cvip == True
    ).all()

    telefonos = list({c.numero_telefono for c in clientes})

    return [{"telefono": tel} for tel in telefonos]


@router.patch("/ventas/{venta_id}/precio")
def editar_precio_venta(
    venta_id: int,
    data: schemas.EditarPrecioVentaRequest,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in (models.RolEnum.admin, models.RolEnum.direccion):
        raise HTTPException(status_code=403, detail="No autorizado")

    venta = db.query(models.Venta).filter(models.Venta.id == venta_id).first()
    if not venta:
        raise HTTPException(status_code=404, detail="Venta no encontrada")

    if venta.cancelada:
        raise HTTPException(status_code=400, detail="No se puede editar el precio de una venta cancelada")

    if data.nuevo_precio <= 0:
        raise HTTPException(status_code=400, detail="El precio debe ser mayor a 0")

    venta.precio_unitario = data.nuevo_precio
    venta.total = data.nuevo_precio * venta.cantidad
    db.commit()

    return {"ok": True, "message": "Precio actualizado correctamente", "nuevo_total": venta.total}


# ── Portabilidades ─────────────────────────────────────────────────────────
# Los chips de portabilidad requieren un tramite posterior a la venta, asi que
# se marcan aparte cuando ya se realizo. Solo lo ve y lo mueve admin/direccion.
# El control del tramite es solo para Cadenas: se deja fuera "Portabilidad" y
# "Portabilidad Coppel".
TIPOS_PORTABILIDAD = ["Porta Otras cadenas"]


def _nombre_asesor(empleado):
    if not empleado:
        return None
    return (empleado.nombre_englobado or "").strip() or empleado.nombre_completo or empleado.username


@router.get("/portabilidades", response_model=List[schemas.PortabilidadItem])
def listar_portabilidades(
    estado: str = "pendientes",
    fecha_inicio: Optional[date] = None,
    fecha_fin: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(
        verificar_rol_requerido([models.RolEnum.admin, models.RolEnum.direccion])
    ),
):
    if estado not in ("pendientes", "realizadas", "todas"):
        raise HTTPException(
            status_code=400,
            detail="estado debe ser 'pendientes', 'realizadas' o 'todas'",
        )

    query = (
        db.query(models.VentaChip)
        .options(
            joinedload(models.VentaChip.empleado).joinedload(models.Usuario.modulo)
        )
        .filter(models.VentaChip.tipo_chip.in_(TIPOS_PORTABILIDAD))
        .filter(models.VentaChip.cancelada == False)  # noqa: E712
        # Las portabilidades registradas antes del cambio de formulario no traen
        # estos datos y no sirven para el control del tramite.
        .filter(models.VentaChip.curp.isnot(None), models.VentaChip.curp != "")
        .filter(models.VentaChip.iccid.isnot(None), models.VentaChip.iccid != "")
        .filter(models.VentaChip.nip.isnot(None), models.VentaChip.nip != "")
    )

    if estado == "pendientes":
        query = query.filter(models.VentaChip.portabilidad_realizada == False)  # noqa: E712
    elif estado == "realizadas":
        query = query.filter(models.VentaChip.portabilidad_realizada == True)  # noqa: E712

    if fecha_inicio:
        query = query.filter(models.VentaChip.fecha >= fecha_inicio)
    if fecha_fin:
        query = query.filter(models.VentaChip.fecha <= fecha_fin)

    filas = (
        query.order_by(models.VentaChip.fecha.desc(), models.VentaChip.hora.desc())
        .limit(500)
        .all()
    )

    return [
        schemas.PortabilidadItem(
            id=c.id,
            fecha=c.fecha,
            hora=c.hora,
            tipo_chip=c.tipo_chip,
            numero_telefono=c.numero_telefono,
            curp=c.curp,
            iccid=c.iccid,
            nip=c.nip,
            asesor=_nombre_asesor(c.empleado),
            modulo=getattr(getattr(c.empleado, "modulo", None), "nombre", None),
            portabilidad_realizada=bool(c.portabilidad_realizada),
            fecha_portabilidad=c.fecha_portabilidad,
        )
        for c in filas
    ]


@router.patch("/portabilidades/{venta_id}", response_model=schemas.PortabilidadItem)
def marcar_portabilidad(
    venta_id: int,
    data: schemas.MarcarPortabilidadRequest,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(
        verificar_rol_requerido([models.RolEnum.admin, models.RolEnum.direccion])
    ),
):
    chip = db.query(models.VentaChip).filter(models.VentaChip.id == venta_id).first()
    if not chip:
        raise HTTPException(status_code=404, detail="Portabilidad no encontrada")

    if data.realizada:
        chip.portabilidad_realizada = True
        chip.fecha_portabilidad = datetime.now(ZoneInfo("America/Mexico_City")).replace(tzinfo=None)
    else:
        chip.portabilidad_realizada = False
        chip.fecha_portabilidad = None

    db.commit()
    db.refresh(chip)

    return schemas.PortabilidadItem(
        id=chip.id,
        fecha=chip.fecha,
        hora=chip.hora,
        tipo_chip=chip.tipo_chip,
        numero_telefono=chip.numero_telefono,
        curp=chip.curp,
        iccid=chip.iccid,
        nip=chip.nip,
        asesor=_nombre_asesor(chip.empleado),
        modulo=getattr(getattr(chip.empleado, "modulo", None), "nombre", None),
        portabilidad_realizada=bool(chip.portabilidad_realizada),
        fecha_portabilidad=chip.fecha_portabilidad,
    )
