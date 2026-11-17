from datetime import date, datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.config import get_current_user
from app.database import get_db
from app.routers.vacaciones import (
    NIVELES,
    VacacionResponse,
    _clave_columna,
    _nombres_aprobadores,
    _normalizar,
    _serializar,
    _usuario_activo,
)

router = APIRouter()

# Vista de "Mi Perfil" para el propio empleado. La persona SIEMPRE sale del
# token: estos endpoints no aceptan clave ni id por parametro.
# Misma regla que Mis datos (frontend) y Nomina (GET /admin/nomina): una persona
# son todas sus cuentas activas con el mismo nombre_englobado; sueldo = suma de
# las cuentas; forma de pago, cuentas y fecha de ingreso de la primera cuenta
# (por id) con sueldo > 0.

ROLES_EMPLEADO = (models.RolEnum.asesor, models.RolEnum.encargado)


class MiPerfilResponse(BaseModel):
    username: str
    clave: str
    nombre_completo: Optional[str] = None
    nombre_englobado: Optional[str] = None
    modulos: List[str]
    sueldo: float
    forma_pago: Optional[str] = None
    cuenta_clabe: Optional[str] = None
    cuenta_interbancaria: Optional[str] = None
    fecha_ingreso: Optional[date] = None


def _clave_de(usuario: models.Usuario) -> str:
    return _normalizar(usuario.nombre_englobado) or _normalizar(usuario.username)


def _cuentas_de(db: Session, usuario: models.Usuario) -> List[models.Usuario]:
    englobado = _normalizar(usuario.nombre_englobado)
    if not englobado:
        return [usuario]
    cuentas = (
        db.query(models.Usuario)
        .filter(
            _clave_columna(models.Usuario.nombre_englobado) == englobado,
            models.Usuario.activo == True,
            models.Usuario.rol.in_(ROLES_EMPLEADO),
        )
        .order_by(models.Usuario.id)
        .all()
    )
    # La cuenta con la que inicio sesion siempre cuenta, aunque su rol no sea de empleado.
    if all(c.id != usuario.id for c in cuentas):
        cuentas = sorted([*cuentas, usuario], key=lambda c: c.id)
    return cuentas


@router.get("", response_model=MiPerfilResponse)
@router.get("/", response_model=MiPerfilResponse, include_in_schema=False)
def mi_perfil(
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(get_current_user),
):
    cuentas = _cuentas_de(db, usuario)
    principal = next((c for c in cuentas if (c.sueldo_base or 0) > 0), cuentas[0])
    nombre = next((c.nombre_completo for c in cuentas if (c.nombre_completo or "").strip()), None)
    modulos: List[str] = []
    for c in cuentas:
        if c.modulo and c.modulo.nombre and c.modulo.nombre not in modulos:
            modulos.append(c.modulo.nombre)
    return {
        "username": usuario.username,
        "clave": _clave_de(usuario),
        "nombre_completo": nombre,
        "nombre_englobado": cuentas[0].nombre_englobado,
        "modulos": modulos,
        "sueldo": round(sum(c.sueldo_base or 0 for c in cuentas), 2),
        "forma_pago": principal.forma_pago,
        "cuenta_clabe": principal.cuenta_clabe,
        "cuenta_interbancaria": principal.cuenta_interbancaria,
        "fecha_ingreso": principal.fecha_ingreso,
    }


@router.get("/vacaciones", response_model=List[VacacionResponse])
def mis_vacaciones(
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(get_current_user),
):
    englobado = _normalizar(usuario.nombre_englobado)
    # La tabla vacaciones usa el nombre_englobado como clave: sin englobado no hay periodos.
    if not englobado:
        return []
    vacaciones = (
        db.query(models.Vacacion)
        .filter(_clave_columna(models.Vacacion.clave) == englobado)
        .order_by(models.Vacacion.fecha_inicio, models.Vacacion.id)
        .all()
    )
    nombres = _nombres_aprobadores(db, vacaciones)
    return [_serializar(v, nombres) for v in vacaciones]


# ------------------- Solicitudes de vacaciones (piloto) -------------------
# Por ahora solo los usernames de esta lista pueden pedir o cancelar vacaciones.
PILOTO_MI_PERFIL = ("A55BO-EVELYN",)

ZONA_MX = ZoneInfo("America/Mexico_City")
# Igual que el calendario del frontend: primer dia elegible = hoy (Mexico) + 31
DIAS_ANTICIPACION = 31
MAX_DIAS_SEGUIDOS = 6
DIAS_ENTRE_PERIODOS = 15
# Periodos que ya cuentan para "uno por mes" y para la separacion de 15 dias
ESTADOS_VIGENTES = ("solicitada", "autorizada", "tomada")

MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


class SolicitudVacacion(BaseModel):
    # extra="forbid": si mandan clave, id o cualquier otro campo, se rechaza (422)
    model_config = ConfigDict(extra="forbid")

    fecha_inicio: date
    fecha_fin: date
    # Opcional: el servidor lo calcula; si viene y no coincide, se rechaza
    dias: Optional[int] = None
    # Opcional: si no lo pone la persona, admin lo asigna con PATCH /vacaciones/{id}/cubre
    cubre_clave: Optional[str] = Field(None, max_length=50)
    nota: Optional[str] = Field(None, max_length=500)


def _es_piloto(usuario: models.Usuario) -> bool:
    return _normalizar(usuario.username) in {_normalizar(u) for u in PILOTO_MI_PERFIL}


def _exigir_piloto(usuario: models.Usuario) -> None:
    if not _es_piloto(usuario):
        raise HTTPException(status_code=403, detail="Esta función todavía no está disponible para tu usuario")


def _texto_fecha(d: date) -> str:
    return f"{d.day} de {MESES[d.month - 1]} de {d.year}"


def _dia_bloqueado(d: date) -> bool:
    # Ventas navideñas (7 dic - 7 ene) y Buen Fin (13 - 16 nov), cualquier año
    return (
        (d.month == 12 and d.day >= 7)
        or (d.month == 1 and d.day <= 7)
        or (d.month == 11 and 13 <= d.day <= 16)
    )


def _meses(inicio: date, fin: date) -> set:
    # Un periodo ocupa el mes donde empieza y el mes donde termina
    return {(inicio.year, inicio.month), (fin.year, fin.month)}


@router.post("/vacaciones", response_model=VacacionResponse, status_code=201)
def pedir_vacaciones(
    body: SolicitudVacacion,
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(get_current_user),
):
    _exigir_piloto(usuario)

    clave = (usuario.nombre_englobado or "").strip()
    if not clave:
        raise HTTPException(status_code=400, detail="Tu usuario no tiene nombre englobado; pide a RH que lo registre")
    clave_norm = _normalizar(clave)

    inicio, fin = body.fecha_inicio, body.fecha_fin
    if fin < inicio:
        raise HTTPException(status_code=400, detail="La fecha final no puede ser anterior a la fecha de inicio")

    dias = (fin - inicio).days + 1
    if body.dias is not None and body.dias != dias:
        raise HTTPException(
            status_code=400,
            detail=f"Los días no coinciden con las fechas: del {_texto_fecha(inicio)} al {_texto_fecha(fin)} son {dias}",
        )
    if dias > MAX_DIAS_SEGUIDOS:
        raise HTTPException(status_code=400, detail=f"Máximo {MAX_DIAS_SEGUIDOS} días seguidos por periodo")

    hoy = datetime.now(ZONA_MX).date()
    primer_dia = hoy + timedelta(days=DIAS_ANTICIPACION)
    if inicio < primer_dia:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Las vacaciones se piden con {DIAS_ANTICIPACION} días de anticipación: "
                f"puedes empezar a partir del {_texto_fecha(primer_dia)}"
            ),
        )

    for n in range(dias):
        d = inicio + timedelta(days=n)
        if _dia_bloqueado(d):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"El {_texto_fecha(d)} está en periodo de ventas (7 dic - 7 ene o 13 - 16 nov): "
                    "no se pueden pedir vacaciones"
                ),
            )

    cubre = None
    if (body.cubre_clave or "").strip():
        cubre = _usuario_activo(db, body.cubre_clave)
        if cubre is None:
            raise HTTPException(status_code=400, detail="La persona que cubre no corresponde a un usuario activo")
        if _normalizar(cubre.nombre_englobado) == clave_norm:
            raise HTTPException(status_code=400, detail="No puedes cubrirte a ti misma(o)")

    # Serializa las solicitudes de una misma persona hasta el commit (mismo candado
    # que POST /vacaciones): sin esto, un doble envio pasaria las validaciones dos veces.
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"vacaciones:{clave_norm}"))))

    existentes = (
        db.query(models.Vacacion)
        .filter(
            _clave_columna(models.Vacacion.clave) == clave_norm,
            models.Vacacion.estado.is_distinct_from("rechazada"),
        )
        .order_by(models.Vacacion.fecha_inicio)
        .all()
    )

    for v in existentes:
        if v.fecha_inicio <= fin and v.fecha_fin >= inicio:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Se encima con tu periodo del {_texto_fecha(v.fecha_inicio)} "
                    f"al {_texto_fecha(v.fecha_fin)} ({v.estado})"
                ),
            )

    # Las faltas injustificadas no cuentan como periodo de vacaciones
    vigentes = [
        v for v in existentes
        if (v.estado or "").strip().lower() in ESTADOS_VIGENTES and v.tipo != "falta_injustificada"
    ]

    meses_nuevos = _meses(inicio, fin)
    for v in vigentes:
        if meses_nuevos & _meses(v.fecha_inicio, v.fecha_fin):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Solo se permite un periodo de vacaciones por mes: ya tienes el del "
                    f"{_texto_fecha(v.fecha_inicio)} al {_texto_fecha(v.fecha_fin)}"
                ),
            )

    for v in vigentes:
        separado = (
            inicio > v.fecha_fin + timedelta(days=DIAS_ENTRE_PERIODOS)
            or fin < v.fecha_inicio - timedelta(days=DIAS_ENTRE_PERIODOS)
        )
        if not separado:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Debe haber al menos {DIAS_ENTRE_PERIODOS} días libres entre un periodo y otro: "
                    f"tienes uno del {_texto_fecha(v.fecha_inicio)} al {_texto_fecha(v.fecha_fin)}"
                ),
            )

    nota = (body.nota or "").strip() or None
    vacacion = models.Vacacion(
        clave=clave,
        fecha_inicio=inicio,
        fecha_fin=fin,
        dias=dias,
        estado="solicitada",
        tipo="vacaciones",
        origen="mi_perfil",
        nota=nota,
        creado_por=usuario.id,
        creado_at=datetime.now(timezone.utc),
        cubre_clave=cubre.nombre_englobado.strip() if cubre else None,
    )
    db.add(vacacion)
    db.commit()
    db.refresh(vacacion)
    return _serializar(vacacion, {})


@router.delete("/vacaciones/{vacacion_id}")
def cancelar_solicitud(
    vacacion_id: int,
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(get_current_user),
):
    _exigir_piloto(usuario)

    clave_norm = _normalizar(usuario.nombre_englobado)
    vacacion = (
        db.query(models.Vacacion)
        .filter(models.Vacacion.id == vacacion_id)
        .with_for_update()
        .first()
    )
    # No existe o es de otra persona: misma respuesta, para no revelar periodos ajenos
    if vacacion is None or not clave_norm or _normalizar(vacacion.clave) != clave_norm:
        raise HTTPException(status_code=403, detail="No puedes cancelar este periodo")

    # Solo las que la persona pidió desde Mi Perfil; las capturadas por admin (sistema) o de Excel no
    if vacacion.origen != "mi_perfil":
        raise HTTPException(status_code=400, detail="Solo puedes cancelar solicitudes que hiciste desde Mi Perfil")
    if vacacion.tipo != "vacaciones" or (vacacion.estado or "").strip().lower() != "solicitada":
        raise HTTPException(status_code=400, detail="Solo se pueden cancelar solicitudes que sigan en estado solicitada")
    if any(getattr(vacacion, f"aprobado_{nivel}") is not None for nivel in NIVELES):
        raise HTTPException(status_code=400, detail="Esta solicitud ya tiene una revisión; ya no se puede cancelar")

    db.delete(vacacion)
    db.commit()
    return {"ok": True}
