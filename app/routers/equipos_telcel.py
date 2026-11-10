import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.params import File
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

from app import models
from app.database import get_db
from app.utilidades import verificar_rol_requerido

router = APIRouter()

# Alta, edición y surtido de IMEIs: solo admin. Los GET quedan como estaban
# (los usa la pantalla de venta de teléfonos).
SOLO_ADMIN = verificar_rol_requerido(models.RolEnum.admin)


class MarcarSurtidosRequest(BaseModel):
    imeis: list[str]
    modulo_id: int
    folio: str | None = None


class AltaIndividualRequest(BaseModel):
    imei: str
    clave: str
    producto: str
    modulo_id: int
    folio: Optional[str] = None


class AltaMultipleRequest(BaseModel):
    equipos: list[AltaIndividualRequest]


class CambiarActivacionRequest(BaseModel):
    activado: bool | None = None
    estado_activacion: str | None = None


class AltaBodegaRequest(BaseModel):
    imei: str
    clave: str
    producto: str
    fecha_compra: str   # formato YYYY-MM-DD


class EditarImeiRequest(BaseModel):
    imei: str


class FechaActivacionRequest(BaseModel):
    fecha_activacion: str | None = None   # "YYYY-MM-DD" o null para limpiar


class FechaEstatusInicialRequest(BaseModel):
    fecha_estatus_inicial: str | None = None


class CumpleArlRequest(BaseModel):
    cumple_arl: bool


class EditarChipCasadoRequest(BaseModel):
    chip_casado: str


@router.post("/upload/")
def upload_equipos_telcel(
    archivo: UploadFile = File(...),
    db: Session = Depends(get_db),
    _admin: models.Usuario = Depends(SOLO_ADMIN)
):
    # 1️⃣ Leer el Excel
    try:
        df = pd.read_excel(archivo.file)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error al leer el archivo Excel: {e}"
        )

    # 2️⃣ Normalizar encabezados a minúsculas sin espacios
    df.columns = [str(c).strip().lower() for c in df.columns]

    # 3️⃣ Validar columnas requeridas
    requeridas = ["imei", "clave", "producto", "fecha_compra"]
    faltantes = [c for c in requeridas if c not in df.columns]
    if faltantes:
        raise HTTPException(
            status_code=400,
            detail=f"Faltan columnas requeridas: {', '.join(faltantes)}"
        )

    insertados = 0
    saltados_repetidos = 0
    claves_no_reconocidas = []

    # 4️⃣ Procesar filas
    for _, fila in df.iterrows():
        imei = str(fila["imei"]).strip()
        clave = str(fila["clave"]).strip()
        producto = str(fila["producto"]).strip()

        try:
            fecha_compra = pd.to_datetime(fila["fecha_compra"]).date()
        except Exception:
            raise HTTPException(
                status_code=400,
                detail=f"Fecha de compra inválida en el IMEI {imei}"
            )

        # 🔒 PROTECCIÓN: saltar IMEI ya existente
        existente = (
            db.query(models.EquiposTelcel)
            .filter(models.EquiposTelcel.imei == imei)
            .first()
        )
        if existente:
            saltados_repetidos += 1
            continue

        # 🔒 VALIDACIÓN: la clave debe existir en el catálogo maestro
        existe_clave = (
            db.query(models.InventarioGeneral)
            .filter(models.InventarioGeneral.clave == clave)
            .first()
        )
        if not existe_clave:
            claves_no_reconocidas.append(clave)
            continue

        db.add(models.EquiposTelcel(
            imei=imei,
            clave=clave,
            producto=producto,
            fecha_compra=fecha_compra
            # estatus: se deja el default 'en_bodega' de la tabla
        ))
        insertados += 1

    db.commit()

    return {
        "status": "success",
        "insertados": insertados,
        "saltados_repetidos": saltados_repetidos,
        "rechazados_clave": len(claves_no_reconocidas),
        "claves_no_reconocidas": sorted(set(claves_no_reconocidas))
    }


@router.get("/")
def listar_equipos(
    estatus: str | None = None,
    producto: str | None = None,
    fecha_inicio: str | None = None,
    fecha_fin: str | None = None,
    db: Session = Depends(get_db)
):
    query = db.query(models.EquiposTelcel)

    if estatus:
        query = query.filter(models.EquiposTelcel.estatus == estatus)
    if producto:
        query = query.filter(models.EquiposTelcel.producto.ilike(f"%{producto}%"))
    if fecha_inicio:
        query = query.filter(models.EquiposTelcel.fecha_compra >= fecha_inicio)
    if fecha_fin:
        query = query.filter(models.EquiposTelcel.fecha_compra <= fecha_fin)

    equipos = query.order_by(models.EquiposTelcel.id.desc()).all()

    modulos = {m.id: m.nombre for m in db.query(models.Modulo.id, models.Modulo.nombre).all()}

    # Numero de linea y clasificacion desde la venta del telefono, en UNA sola query.
    imeis = [e.imei for e in equipos if e.imei]
    ventas_por_imei = {}
    if imeis:
        ventas_rows = (
            db.query(models.Venta.imei, models.Venta.chip_casado, models.Venta.clasificacion)
            .filter(models.Venta.imei.in_(imeis))
            .filter(models.Venta.tipo_producto == "telefono")
            .filter(models.Venta.cancelada == False)  # noqa: E712
            .filter(models.Venta.clasificacion.in_(["linea_nueva", "boletin_63", "chip_ato", "chip_promo"]))
            .order_by(models.Venta.id.desc())
            .all()
        )
        # Al venir ordenadas por id desc, la primera de cada IMEI es la mas reciente.
        for v in ventas_rows:
            if v.imei not in ventas_por_imei:
                ventas_por_imei[v.imei] = {"numero": v.chip_casado, "clasificacion": v.clasificacion}

    # Vendedor: equipos_telcel.folio_venta -> ventas.folio -> usuarios.
    # El folio es texto libre de los dos lados (no hay FK), asi que el match va
    # normalizado con upper+trim en ambos. Solo se resuelve para vendidos: las
    # otras pantallas que pegan a este endpoint (VentasPage con estatus=surtido)
    # no pagan la query extra.
    def _folio_norm(equipo):
        if equipo.estatus != "vendido" or not equipo.folio_venta:
            return None
        return equipo.folio_venta.strip().upper() or None

    vendedor_por_folio = {}
    folios = {f for f in (_folio_norm(e) for e in equipos) if f}
    if folios:
        folio_col = func.upper(func.trim(models.Venta.folio))
        vendedor_rows = (
            db.query(
                folio_col.label("folio"),
                models.Usuario.nombre_englobado,
                models.Usuario.nombre_completo,
                models.Usuario.username,
            )
            .join(models.Usuario, models.Venta.empleado_id == models.Usuario.id)
            .filter(folio_col.in_(list(folios)))
            .filter(models.Venta.cancelada == False)  # noqa: E712
            .order_by(models.Venta.id.desc())
            .all()
        )
        # Mismo criterio que ventas_por_imei: al venir por id desc, la primera
        # de cada folio es la mas reciente.
        for v in vendedor_rows:
            if v.folio not in vendedor_por_folio:
                nombre = (v.nombre_englobado or "").strip() or v.nombre_completo or v.username or ""
                vendedor_por_folio[v.folio] = nombre.strip().upper()

    return [
        {
            "id": e.id,
            "imei": e.imei,
            "clave": e.clave,
            "producto": e.producto,
            "fecha_compra": str(e.fecha_compra) if e.fecha_compra is not None else None,
            "estatus": e.estatus,
            "modulo_id": e.modulo_id,
            "modulo_nombre": modulos.get(e.modulo_id),
            "fecha_salida": str(e.fecha_salida) if e.fecha_salida is not None else None,
            "fecha_venta": str(e.fecha_venta) if e.fecha_venta is not None else None,
            "fecha_activacion": str(e.fecha_activacion) if e.fecha_activacion is not None else None,
            "fecha_estatus_inicial": str(e.fecha_estatus_inicial) if e.fecha_estatus_inicial is not None else None,
            "numero_linea": (ventas_por_imei.get(e.imei) or {}).get("numero"),
            "clasificacion_venta": (ventas_por_imei.get(e.imei) or {}).get("clasificacion"),
            "cumple_arl": e.cumple_arl,
            "activado": e.activado,
            "estado_activacion": e.estado_activacion,
            "vendedor": vendedor_por_folio.get(_folio_norm(e)) or "—",
        }
        for e in equipos
    ]


@router.get("/buscar-imei/{imei}")
def buscar_por_imei(imei: str, db: Session = Depends(get_db)):
    imei = imei.strip()

    equipo = (
        db.query(models.EquiposTelcel)
        .filter(models.EquiposTelcel.imei == imei)
        .first()
    )
    if not equipo:
        raise HTTPException(
            status_code=404,
            detail=f"IMEI {imei} no está registrado en bodega"
        )

    if equipo.estatus != "en_bodega":
        raise HTTPException(
            status_code=409,
            detail=f"El equipo con IMEI {imei} ya fue surtido (estatus: {equipo.estatus})"
        )

    prod = (
        db.query(models.InventarioGeneral)
        .filter(models.InventarioGeneral.clave == equipo.clave)
        .first()
    )
    if not prod:
        raise HTTPException(
            status_code=404,
            detail=f"La clave {equipo.clave} del equipo no existe en el catálogo"
        )

    return {
        "id": equipo.id,
        "imei": equipo.imei,
        "clave": equipo.clave,
        "producto": equipo.producto,
        "producto_id": prod.id,
        "estatus": equipo.estatus,
    }


@router.post("/marcar-surtidos")
def marcar_surtidos(data: MarcarSurtidosRequest, db: Session = Depends(get_db), _admin: models.Usuario = Depends(SOLO_ADMIN)):
    if not data.imeis:
        return {"status": "success", "marcados": 0, "no_encontrados": [], "ya_surtidos": []}

    ahora = datetime.now(ZoneInfo("America/Mexico_City")).replace(tzinfo=None)
    marcados = 0
    no_encontrados = []
    ya_surtidos = []

    for imei in data.imeis:
        imei_limpio = str(imei).strip()
        equipo = (
            db.query(models.EquiposTelcel)
            .filter(models.EquiposTelcel.imei == imei_limpio)
            .first()
        )
        if not equipo:
            no_encontrados.append(imei_limpio)
            continue
        if equipo.estatus != "en_bodega":
            ya_surtidos.append(imei_limpio)
            continue
        equipo.estatus = "surtido"
        equipo.modulo_id = data.modulo_id
        equipo.fecha_salida = ahora
        equipo.folio = data.folio
        marcados += 1

    db.commit()

    return {
        "status": "success",
        "marcados": marcados,
        "no_encontrados": no_encontrados,
        "ya_surtidos": ya_surtidos,
    }


@router.get("/faltantes-imei/{modulo_id}")
def faltantes_imei(modulo_id: int, db: Session = Depends(get_db)):
    # 1. Teléfonos en existencia del módulo (por cantidad)
    telefonos = (
        db.query(models.InventarioModulo)
        .filter(models.InventarioModulo.modulo_id == modulo_id)
        .filter(models.InventarioModulo.tipo_producto == 'telefono')
        .filter(models.InventarioModulo.cantidad > 0)
        .all()
    )
    # 2. Equipos ya con IMEI en este módulo, agrupados por clave (solo los que están en el módulo: surtido)
    equipos_por_clave = {}
    equipos = (
        db.query(models.EquiposTelcel)
        .filter(models.EquiposTelcel.modulo_id == modulo_id)
        .filter(models.EquiposTelcel.estatus == 'surtido')
        .all()
    )
    for e in equipos:
        equipos_por_clave.setdefault(e.clave, []).append(e)
    # 3. Construir la lista: por cada modelo, `cantidad` filas.
    #    Las primeras se llenan con los equipos que ya tienen IMEI; el resto van vacías.
    filas = []
    for t in telefonos:
        ya = equipos_por_clave.get(t.clave, [])
        for i in range(t.cantidad):
            if i < len(ya):
                eq = ya[i]
                filas.append({
                    "clave": t.clave,
                    "producto": t.producto,
                    "modulo_id": modulo_id,
                    "imei": eq.imei,
                    "equipo_id": eq.id,
                    "tiene_imei": True,
                    "activado": eq.activado,
                })
            else:
                filas.append({
                    "clave": t.clave,
                    "producto": t.producto,
                    "modulo_id": modulo_id,
                    "imei": None,
                    "equipo_id": None,
                    "tiene_imei": False,
                    "activado": False,
                })
    return filas


def _fecha_entrada_real(db, producto: str, modulo_id: int):
    # 1. Kardex: última ENTRADA de ese producto en ese módulo
    mov = (
        db.query(models.KardexMovimiento)
        .filter(models.KardexMovimiento.producto == producto)
        .filter(models.KardexMovimiento.modulo_destino_id == modulo_id)
        .filter(models.KardexMovimiento.tipo_movimiento.in_(["ENTRADA", "TRASPASO_ENTRADA"]))
        .order_by(models.KardexMovimiento.id.desc())
        .first()
    )
    if mov and mov.fecha:
        f = mov.fecha
        return f.replace(tzinfo=None) if f.tzinfo else f
    # 2. Conteo físico más reciente del módulo
    conteo = (
        db.query(models.ConteoFisico)
        .filter(models.ConteoFisico.modulo_id == modulo_id)
        .order_by(models.ConteoFisico.fecha.desc())
        .first()
    )
    if conteo and conteo.fecha:
        f = conteo.fecha
        return f.replace(tzinfo=None) if f.tzinfo else f
    # 3. Hoy
    return datetime.now(ZoneInfo("America/Mexico_City")).replace(tzinfo=None)


@router.post("/alta-individual")
def alta_individual(data: AltaIndividualRequest, db: Session = Depends(get_db), _admin: models.Usuario = Depends(SOLO_ADMIN)):
    imei = data.imei.strip()
    if not imei:
        raise HTTPException(status_code=400, detail="El IMEI no puede estar vacío")
    # No permitir IMEI duplicado
    existe = db.query(models.EquiposTelcel).filter(models.EquiposTelcel.imei == imei).first()
    if existe:
        raise HTTPException(status_code=400, detail=f"El IMEI {imei} ya está registrado")
    ahora = datetime.now(ZoneInfo("America/Mexico_City")).replace(tzinfo=None)
    equipo = models.EquiposTelcel(
        imei=imei,
        clave=data.clave.strip(),
        producto=data.producto.strip(),
        fecha_compra=ahora.date(),
        estatus="surtido",
        modulo_id=data.modulo_id,
        fecha_salida=_fecha_entrada_real(db, data.producto.strip(), data.modulo_id),
        folio=data.folio,
    )
    db.add(equipo)
    db.commit()
    db.refresh(equipo)
    return {"status": "success", "id": equipo.id, "imei": equipo.imei}


@router.post("/alta-multiple")
def alta_multiple(data: AltaMultipleRequest, db: Session = Depends(get_db), _admin: models.Usuario = Depends(SOLO_ADMIN)):
    if not data.equipos:
        return {"status": "success", "guardados": 0, "errores": []}
    ahora = datetime.now(ZoneInfo("America/Mexico_City")).replace(tzinfo=None)
    guardados = 0
    errores = []
    for eq in data.equipos:
        imei = eq.imei.strip()
        if not imei:
            errores.append({"imei": "", "motivo": "IMEI vacío"})
            continue
        existe = db.query(models.EquiposTelcel).filter(models.EquiposTelcel.imei == imei).first()
        if existe:
            errores.append({"imei": imei, "motivo": "ya registrado"})
            continue
        db.add(models.EquiposTelcel(
            imei=imei,
            clave=eq.clave.strip(),
            producto=eq.producto.strip(),
            fecha_compra=ahora.date(),
            estatus="surtido",
            modulo_id=eq.modulo_id,
            fecha_salida=_fecha_entrada_real(db, eq.producto.strip(), eq.modulo_id),
            folio=eq.folio,
        ))
        guardados += 1
    db.commit()
    return {"status": "success", "guardados": guardados, "errores": errores}


ESTADOS_ACTIVACION = ("no_activado", "activado", "libre", "bloqueado")

@router.post("/activar/{equipo_id}")
def cambiar_activacion(equipo_id: int, data: CambiarActivacionRequest, db: Session = Depends(get_db), _admin: models.Usuario = Depends(SOLO_ADMIN)):
    equipo = db.query(models.EquiposTelcel).filter(models.EquiposTelcel.id == equipo_id).first()
    if not equipo:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")

    if data.estado_activacion is not None:
        nuevo = data.estado_activacion.strip().lower()
        if nuevo not in ESTADOS_ACTIVACION:
            raise HTTPException(status_code=400, detail=f"Estado inválido. Valores permitidos: {', '.join(ESTADOS_ACTIVACION)}")
    elif data.activado is not None:
        nuevo = "activado" if data.activado else "no_activado"
    else:
        raise HTTPException(status_code=400, detail="Debes enviar estado_activacion o activado")

    equipo.estado_activacion = nuevo
    equipo.activado = (nuevo == "activado")
    db.commit()
    return {
        "status": "success",
        "id": equipo.id,
        "estado_activacion": equipo.estado_activacion,
        "activado": equipo.activado,
    }


@router.post("/alta-bodega")
def alta_bodega(data: AltaBodegaRequest, db: Session = Depends(get_db), _admin: models.Usuario = Depends(SOLO_ADMIN)):
    imei = data.imei.strip()
    if not imei:
        raise HTTPException(status_code=400, detail="El IMEI no puede estar vacío")
    # IMEI duplicado
    existe = db.query(models.EquiposTelcel).filter(models.EquiposTelcel.imei == imei).first()
    if existe:
        raise HTTPException(status_code=400, detail=f"El IMEI {imei} ya está registrado")
    clave = data.clave.strip()
    # La clave debe existir en el catálogo
    existe_clave = db.query(models.InventarioGeneral).filter(models.InventarioGeneral.clave == clave).first()
    if not existe_clave:
        raise HTTPException(status_code=400, detail=f"La clave {clave} no existe en el catálogo")
    # Parsear fecha
    try:
        from datetime import date as _date
        fecha = _date.fromisoformat(data.fecha_compra.strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Fecha de compra inválida (usa YYYY-MM-DD)")
    equipo = models.EquiposTelcel(
        imei=imei,
        clave=clave,
        producto=data.producto.strip(),
        fecha_compra=fecha,
        # estatus: se deja el default 'en_bodega' de la tabla
    )
    db.add(equipo)
    db.commit()
    db.refresh(equipo)
    return {"status": "success", "id": equipo.id, "imei": equipo.imei, "estatus": equipo.estatus}


@router.post("/editar-imei/{equipo_id}")
def editar_imei(equipo_id: int, data: EditarImeiRequest, db: Session = Depends(get_db), _admin: models.Usuario = Depends(SOLO_ADMIN)):
    equipo = db.query(models.EquiposTelcel).filter(models.EquiposTelcel.id == equipo_id).first()
    if not equipo:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")
    nuevo = data.imei.strip()
    if not nuevo:
        raise HTTPException(status_code=400, detail="El IMEI no puede estar vacío")
    # Si el IMEI nuevo ya lo tiene OTRO equipo, rechazar
    otro = (
        db.query(models.EquiposTelcel)
        .filter(models.EquiposTelcel.imei == nuevo)
        .filter(models.EquiposTelcel.id != equipo_id)
        .first()
    )
    if otro:
        raise HTTPException(status_code=400, detail=f"El IMEI {nuevo} ya está registrado en otro equipo")
    equipo.imei = nuevo
    db.commit()
    return {"status": "success", "id": equipo.id, "imei": equipo.imei}


@router.post("/fecha-activacion/{equipo_id}")
def set_fecha_activacion(equipo_id: int, data: FechaActivacionRequest, db: Session = Depends(get_db), _admin: models.Usuario = Depends(SOLO_ADMIN)):
    equipo = db.query(models.EquiposTelcel).filter(models.EquiposTelcel.id == equipo_id).first()
    if not equipo:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")
    valor = (data.fecha_activacion or "").strip()
    if not valor:
        equipo.fecha_activacion = None
    else:
        try:
            from datetime import date as _date
            equipo.fecha_activacion = _date.fromisoformat(valor)
        except Exception:
            raise HTTPException(status_code=400, detail="Fecha inválida (usa YYYY-MM-DD)")
    db.commit()
    return {"status": "success", "id": equipo.id, "fecha_activacion": str(equipo.fecha_activacion) if equipo.fecha_activacion else None}


@router.post("/fecha-estatus-inicial/{equipo_id}")
def set_fecha_estatus_inicial(equipo_id: int, data: FechaEstatusInicialRequest, db: Session = Depends(get_db), _admin: models.Usuario = Depends(SOLO_ADMIN)):
    equipo = db.query(models.EquiposTelcel).filter(models.EquiposTelcel.id == equipo_id).first()
    if not equipo:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")
    valor = (data.fecha_estatus_inicial or "").strip()
    if not valor:
        equipo.fecha_estatus_inicial = None
    else:
        try:
            from datetime import date as _date
            equipo.fecha_estatus_inicial = _date.fromisoformat(valor)
        except Exception:
            raise HTTPException(status_code=400, detail="Fecha inválida (usa YYYY-MM-DD)")
    db.commit()
    return {"status": "success", "id": equipo.id, "fecha_estatus_inicial": str(equipo.fecha_estatus_inicial) if equipo.fecha_estatus_inicial else None}


@router.post("/cumple-arl/{equipo_id}")
def set_cumple_arl(equipo_id: int, data: CumpleArlRequest, db: Session = Depends(get_db), _admin: models.Usuario = Depends(SOLO_ADMIN)):
    equipo = db.query(models.EquiposTelcel).filter(models.EquiposTelcel.id == equipo_id).first()
    if not equipo:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")
    equipo.cumple_arl = data.cumple_arl
    db.commit()
    return {"status": "success", "id": equipo.id, "cumple_arl": equipo.cumple_arl}


# Vive en el router de equipos pero escribe en ventas: chip_casado es la fuente
# de la columna "Numero" que se muestra en la pantalla de equipos.
@router.post("/editar-chip-casado/{equipo_id}")
def editar_chip_casado(equipo_id: int, data: EditarChipCasadoRequest, db: Session = Depends(get_db), _admin: models.Usuario = Depends(SOLO_ADMIN)):
    equipo = db.query(models.EquiposTelcel).filter(models.EquiposTelcel.id == equipo_id).first()
    if not equipo:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")
    nuevo = (data.chip_casado or "").strip()
    if not nuevo.isdigit() or len(nuevo) != 10:
        raise HTTPException(status_code=400, detail="El número debe tener exactamente 10 dígitos")
    # Misma venta que resuelve el GET /: la mas reciente del IMEI, no cancelada,
    # de telefono y con clasificacion que lleva chip casado.
    venta = (
        db.query(models.Venta)
        .filter(models.Venta.imei == equipo.imei)
        .filter(models.Venta.tipo_producto == "telefono")
        .filter(models.Venta.cancelada == False)  # noqa: E712
        .filter(models.Venta.clasificacion.in_(["linea_nueva", "boletin_63", "chip_ato", "chip_promo"]))
        .order_by(models.Venta.id.desc())
        .first()
    )
    if not venta:
        raise HTTPException(
            status_code=404,
            detail=f"No hay venta de teléfono activa para el IMEI {equipo.imei}",
        )
    venta.chip_casado = nuevo
    db.commit()
    return {"status": "success", "id": equipo.id, "imei": equipo.imei, "venta_id": venta.id, "chip_casado": venta.chip_casado}
