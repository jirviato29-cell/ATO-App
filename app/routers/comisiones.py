from datetime import date, datetime, time, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app import models, schemas
from app.config import get_current_user
from app.congelamiento import aplicar_congelamiento, dias_congelados_batch
from app.database import get_db
from app.utilidades import verificar_rol_requerido



router = APIRouter()

# CREAR COMISIÓN
@router.post("/comisiones", response_model=schemas.ComisionResponse)
def crear_comision(
    comision: schemas.ComisionCreate,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(verificar_rol_requerido(models.RolEnum.admin))
):

    existente = db.query(models.Comision).filter_by(producto=comision.producto).first()
    if existente:
        raise HTTPException(status_code=400, detail="Este producto ya tiene comisión registrada")

    nueva = models.Comision(**comision.dict())
    db.add(nueva)
    db.commit()
    db.refresh(nueva)
    return nueva


# EDITAR COMISIÓN
@router.put("/comisiones/{producto}", response_model=schemas.ComisionResponse)
def actualizar_comision(
    producto: str,
    comision: schemas.ComisionUpdate,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(verificar_rol_requerido(models.RolEnum.admin))
):

    com_db = db.query(models.Comision).filter_by(producto=producto).first()
    if not com_db:
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    com_db.cantidad = comision.cantidad
    db.commit()
    db.refresh(com_db)
    return com_db


# ELIMINAR COMISIÓN
@router.delete("/comisiones/{producto}")
def eliminar_comision(
    producto: str,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(verificar_rol_requerido(models.RolEnum.admin))
):

    com_db = db.query(models.Comision).filter_by(producto=producto).first()
    if not com_db:
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    com_db.activo = False
    db.commit()
    return {"mensaje": f"Comisión para producto '{producto}' eliminada"}



@router.get("/comisiones", response_model=list[schemas.ComisionCreate])
def obtener_comisiones(db: Session = Depends(get_db), user: models.Usuario = Depends(get_current_user)):
    
    return db.query(models.Comision).filter(models.Comision.activo == True).all()

@router.get("/comisiones/{producto}", response_model=schemas.ComisionCreate | None)
def obtener_comision_producto(producto: str, db: Session = Depends(get_db), user: models.Usuario = Depends(get_current_user)):
    return db.query(models.Comision).filter_by(producto=producto).first()







def calcular_comisiones(db, empleado_id, inicio, fin):

    fecha_pago = fin + timedelta(days=3)

    ventas = db.query(models.Venta).filter(
        models.Venta.empleado_id == empleado_id,
        models.Venta.fecha >= datetime.combine(inicio, datetime.min.time()),
        models.Venta.fecha <= datetime.combine(fin, datetime.max.time()),
        models.Venta.cancelada == False
    ).all()

    ventas_chips = db.query(models.VentaChip).filter(
        models.VentaChip.empleado_id == empleado_id,
        models.VentaChip.cancelada == False,
        models.VentaChip.es_incubadora == False,
        models.VentaChip.validado == True,
        models.VentaChip.numero_telefono.isnot(None),
        models.VentaChip.fecha >= datetime.combine(inicio, datetime.min.time()),
        models.VentaChip.fecha <= datetime.combine(fin, datetime.max.time()),
    ).all()

    accesorios = []
    telefonos = []
    chips = []

    total_accesorios = 0
    total_telefonos = 0
    total_chips = 0

    comisiones_por_tipo = {
        "contado": 10,
        "paguitos": 110,
        "pajoy": 100
    }

    # Una sola llamada por request, nunca dentro de los bucles de abajo.
    congelados = dias_congelados_batch(db, [empleado_id], inicio, fin).get(empleado_id, set())
    fechas_congeladas = set()

    for v in ventas:

        comision_base = (v.comision_monto if v.comision_monto is not None
                         else getattr(getattr(v, "comision_obj", None), "cantidad", 0) or 0)
        cantidad = getattr(v, "cantidad", 0) or 0
        tipo_venta = (getattr(v, "tipo_venta", "") or "").strip().lower()
        esta_congelado = v.fecha in congelados if v.fecha else False

        if getattr(v, "tipo_producto", "") == "telefono":
            if tipo_venta == "contado" and comision_base > 0:
                comision_total = comision_base * cantidad
            elif v.comision_monto is not None and v.comision_monto == 0 and v.comision_id is None:
                # Vendido abajo del precio: sin los $10 de Contado.
                comision_total = 0
            else:
                comision_extra = comisiones_por_tipo.get(tipo_venta, 0)
                comision_total = (comision_base + comision_extra) * cantidad
            comision_total, congelado = aplicar_congelamiento(comision_total, esta_congelado)
            if congelado:
                fechas_congeladas.add(v.fecha)
            total_telefonos += comision_total
            telefonos.append({
                "producto": v.producto,
                "cantidad": cantidad,
                "comision": comision_base,
                "comision_total": comision_total,
                "tipo_venta": tipo_venta,
                "fecha": v.fecha if v.fecha else None,
                "hora": v.hora if v.hora else None,
                "congelado": congelado,
            })

        elif getattr(v, "tipo_producto", "") == "accesorios":
            comision_total, congelado = aplicar_congelamiento(
                comision_base * cantidad, esta_congelado
            )
            if congelado:
                fechas_congeladas.add(v.fecha)
            total_accesorios += comision_total
            accesorios.append({
                "producto": v.producto,
                "cantidad": cantidad,
                "comision": comision_base,
                "tipo_venta": tipo_venta,
                "comision_total": comision_total,
                "fecha": v.fecha if v.fecha else None,
                "hora": v.hora if v.hora else None,
                "congelado": congelado,
            })

    for v in ventas_chips:
        esta_congelado = v.fecha in congelados if v.fecha else False
        comision, congelado = aplicar_congelamiento(float(v.comision or 0), esta_congelado)
        if congelado:
            fechas_congeladas.add(v.fecha)
        total_chips += comision
        chips.append({
            "tipo_chip": v.tipo_chip,
            "numero_telefono": v.numero_telefono,
            "comision": comision,
            "es_incubadora": bool(v.es_incubadora),
            "fecha": v.fecha if v.fecha else None,
            "hora": v.hora if v.hora else None,
            "congelado": congelado,
        })

    total_general = total_accesorios + total_telefonos + total_chips
    return {
        "inicio_ciclo": inicio,
        "fin_ciclo": fin,
        "fecha_pago": fecha_pago,
        "total_chips": total_chips,
        "total_accesorios": total_accesorios,
        "total_telefonos": total_telefonos,
        "total_general": total_general,
        "ventas_accesorios": accesorios,
        "ventas_telefonos": telefonos,
        "ventas_chips": chips,
        "dias_congelados": len(fechas_congeladas),
        "congelado": bool(fechas_congeladas),
    }

@router.get("/ciclo_por_fechas", response_model=schemas.ComisionesCicloResponse)
def obtener_comisiones_por_fechas(
    inicio: date = Query(...),
    fin: date = Query(...),
    empleado_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):

    empleado = empleado_id or current_user.id

    return calcular_comisiones(db, empleado, inicio, fin)



@router.get("/comisiones/ciclo", response_model=schemas.ComisionesCicloResponse)
def obtener_comisiones_ciclo(
    empleado_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user)
):

    hoy = datetime.now()
    dias_desde_lunes = hoy.weekday()

    inicio = (hoy - timedelta(days=dias_desde_lunes)).date()
    fin = inicio + timedelta(days=6)

    # 🔹 Lógica de roles
    if current_user.rol == models.RolEnum.admin:
        empleado = empleado_id if empleado_id else current_user.id
    else:
        empleado = current_user.id

    return calcular_comisiones(db, empleado, inicio, fin)