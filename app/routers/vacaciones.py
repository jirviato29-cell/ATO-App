import math
import unicodedata
from typing import Dict, Iterable, List, Literal, Optional
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.utilidades import verificar_rol_requerido

router = APIRouter()

# La tabla vacaciones vive en la BD y se altera a mano: aqui no hay migraciones.
# La clave de cada periodo es el nombre_englobado de la persona y siempre se
# compara con UPPER(TRIM()), porque los datos cargados de Excel no son uniformes.

NIVELES = ("rh", "encargado", "gerencia")

solo_admin = verificar_rol_requerido(models.RolEnum.admin)


class VacacionResponse(BaseModel):
    id: int
    clave: str
    fecha_inicio: date
    fecha_fin: date
    dias: Optional[float] = None
    estado: Optional[str] = None
    origen: Optional[str] = None
    nota: Optional[str] = None
    tipo: Optional[str] = None
    aprobado_rh: Optional[bool] = None
    aprobado_rh_por: Optional[str] = None
    aprobado_rh_at: Optional[datetime] = None
    aprobado_encargado: Optional[bool] = None
    aprobado_encargado_por: Optional[str] = None
    aprobado_encargado_at: Optional[datetime] = None
    aprobado_gerencia: Optional[bool] = None
    aprobado_gerencia_por: Optional[str] = None
    aprobado_gerencia_at: Optional[datetime] = None
    cubre_clave: Optional[str] = None


class SolicitudResponse(VacacionResponse):
    nombre_completo: Optional[str] = None
    modulos: List[str] = []
    # Saldo del ciclo vigente (aniversario a aniversario); null sin fecha_ingreso
    dias_corresponden: Optional[int] = None
    ciclo_inicio: Optional[date] = None
    ciclo_fin: Optional[date] = None
    dias_usados: Optional[float] = None
    dias_quedan: Optional[float] = None


class VacacionCrear(BaseModel):
    clave: str = Field(..., min_length=1, max_length=50)
    fecha_inicio: date
    fecha_fin: date
    dias: float = Field(..., gt=0, le=999.9)
    tipo: Literal["vacaciones", "falta_injustificada"]
    nota: Optional[str] = None


class AprobacionBody(BaseModel):
    nivel: Literal["rh", "encargado", "gerencia"]
    aprobado: bool


class CubreBody(BaseModel):
    cubre_clave: Optional[str]


def _normalizar(clave: Optional[str]) -> str:
    return (clave or "").strip().upper()


def _clave_columna(columna):
    return func.upper(func.trim(columna))


def _usuario_activo(db: Session, clave: str) -> Optional[models.Usuario]:
    clave_norm = _normalizar(clave)
    # Sin esto, una clave en blanco empataria con los usuarios sin englobar.
    if not clave_norm:
        return None
    return (
        db.query(models.Usuario)
        .filter(
            _clave_columna(models.Usuario.nombre_englobado) == clave_norm,
            models.Usuario.activo == True,
        )
        .order_by(models.Usuario.id)
        .first()
    )


def _nombres_aprobadores(db: Session, vacaciones: Iterable[models.Vacacion]) -> Dict[int, str]:
    ids = {
        getattr(v, f"aprobado_{nivel}_por")
        for v in vacaciones
        for nivel in NIVELES
    } - {None}
    if not ids:
        return {}
    filas = (
        db.query(models.Usuario.id, models.Usuario.nombre_englobado, models.Usuario.username)
        .filter(models.Usuario.id.in_(ids))
        .all()
    )
    return {f.id: (f.nombre_englobado or "").strip() or f.username for f in filas}


def _serializar(v: models.Vacacion, nombres: Dict[int, str]) -> dict:
    datos = {
        "id": v.id,
        "clave": v.clave,
        "fecha_inicio": v.fecha_inicio,
        "fecha_fin": v.fecha_fin,
        "dias": v.dias,
        "estado": v.estado,
        "origen": v.origen,
        "nota": v.nota,
        "tipo": v.tipo,
        "cubre_clave": v.cubre_clave,
    }
    for nivel in NIVELES:
        por = getattr(v, f"aprobado_{nivel}_por")
        datos[f"aprobado_{nivel}"] = getattr(v, f"aprobado_{nivel}")
        datos[f"aprobado_{nivel}_por"] = nombres.get(por) if por is not None else None
        datos[f"aprobado_{nivel}_at"] = getattr(v, f"aprobado_{nivel}_at")
    return datos


def _obtener_vacacion(db: Session, vacacion_id: int, bloquear: bool = False) -> models.Vacacion:
    query = db.query(models.Vacacion).filter(models.Vacacion.id == vacacion_id)
    if bloquear:
        query = query.with_for_update()
    vacacion = query.first()
    if vacacion is None:
        raise HTTPException(status_code=404, detail="Periodo no encontrado")
    return vacacion


@router.get("", response_model=List[VacacionResponse])
@router.get("/", response_model=List[VacacionResponse], include_in_schema=False)
def listar_vacaciones(
    clave: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    _admin: models.Usuario = Depends(solo_admin),
):
    vacaciones = (
        db.query(models.Vacacion)
        .filter(_clave_columna(models.Vacacion.clave) == _normalizar(clave))
        .order_by(models.Vacacion.fecha_inicio, models.Vacacion.id)
        .all()
    )
    nombres = _nombres_aprobadores(db, vacaciones)
    return [_serializar(v, nombres) for v in vacaciones]


@router.get("/solicitudes", response_model=List[SolicitudResponse])
def listar_solicitudes(
    db: Session = Depends(get_db),
    _admin: models.Usuario = Depends(solo_admin),
):
    vacaciones = (
        db.query(models.Vacacion)
        .filter(
            models.Vacacion.tipo == "vacaciones",
            models.Vacacion.estado == "solicitada",
        )
        .order_by(models.Vacacion.fecha_inicio, models.Vacacion.id)
        .all()
    )
    if not vacaciones:
        return []

    claves = {_normalizar(v.clave) for v in vacaciones}
    clave_usuario = _clave_columna(models.Usuario.nombre_englobado)
    # Una persona puede tener varias cuentas: las activas van al final para que
    # su nombre sobreescriba al de cuentas dadas de baja.
    filas = (
        db.query(clave_usuario.label("clave"), models.Usuario.nombre_completo)
        .filter(clave_usuario.in_(claves))
        .order_by(models.Usuario.activo.is_(True), models.Usuario.id.desc())
        .all()
    )
    nombre_por_clave = {f.clave: f.nombre_completo for f in filas}
    saldos = _saldos_vacaciones(db, claves)

    nombres = _nombres_aprobadores(db, vacaciones)
    return [
        {
            **_serializar(v, nombres),
            "nombre_completo": nombre_por_clave.get(_normalizar(v.clave)),
            **saldos.get(_normalizar(v.clave), _SALDO_VACIO),
        }
        for v in vacaciones
    ]


# ------------------- Saldo de vacaciones (solo lectura) -------------------
ZONA_MX = ZoneInfo("America/Mexico_City")
ROLES_EMPLEADO = (models.RolEnum.asesor, models.RolEnum.encargado)
_SALDO_VACIO = {
    "modulos": [],
    "dias_corresponden": None,
    "ciclo_inicio": None,
    "ciclo_fin": None,
    "dias_usados": None,
    "dias_quedan": None,
}


def _dias_lft(anios: int) -> int:
    # LFT: 1=12, 2=14, 3=16, 4=18, 5=20; de ahi +2 cada 5 anios (6-10=22, 11-15=24, ...)
    if anios <= 0:
        return 0
    if anios <= 5:
        return 10 + 2 * anios
    return 20 + 2 * math.ceil((anios - 5) / 5)


def _aniversario(ingreso: date, anio: int) -> date:
    # Quien entro un 29 de febrero cumple el 28 en anios no bisiestos
    try:
        return ingreso.replace(year=anio)
    except ValueError:
        return date(anio, 2, 28)


def _sin_acentos(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto or "") if unicodedata.category(c) != "Mn"
    ).strip().lower()


def _solo_fin_de_semana(horario) -> bool:
    # Dias trabajados = los que no son descanso y tienen entrada y salida
    if not isinstance(horario, list):
        return False
    trabajados = {
        _sin_acentos(str(d.get("dia", "")))
        for d in horario
        if isinstance(d, dict) and not d.get("descanso") and d.get("entrada") and d.get("salida")
    }
    return bool(trabajados) and trabajados <= {"sabado", "domingo"}


def _saldos_vacaciones(db: Session, claves: Iterable[str]) -> Dict[str, dict]:
    claves = {c for c in claves if c}
    if not claves:
        return {}
    hoy = datetime.now(ZONA_MX).date()

    # Misma regla que Mis datos / Mi Perfil: cuentas activas de empleado con ese
    # nombre_englobado; la principal es la primera (por id) con sueldo > 0.
    cuentas_por_clave: Dict[str, List[models.Usuario]] = {}
    for u in (
        db.query(models.Usuario)
        .filter(
            _clave_columna(models.Usuario.nombre_englobado).in_(claves),
            models.Usuario.activo == True,
            models.Usuario.rol.in_(ROLES_EMPLEADO),
        )
        .order_by(models.Usuario.id)
        .all()
    ):
        cuentas_por_clave.setdefault(_normalizar(u.nombre_englobado), []).append(u)

    aprobadas: Dict[str, List[models.Vacacion]] = {}
    for v in (
        db.query(models.Vacacion)
        .filter(
            _clave_columna(models.Vacacion.clave).in_(claves),
            models.Vacacion.tipo == "vacaciones",
            func.lower(func.trim(models.Vacacion.estado)).in_(("autorizada", "tomada")),
        )
        .all()
    ):
        aprobadas.setdefault(_normalizar(v.clave), []).append(v)

    saldos: Dict[str, dict] = {}
    for clave, cuentas in cuentas_por_clave.items():
        modulos: List[str] = []
        for c in cuentas:
            if c.modulo and c.modulo.nombre and c.modulo.nombre not in modulos:
                modulos.append(c.modulo.nombre)
        saldo = {**_SALDO_VACIO, "modulos": modulos}

        principal = next((c for c in cuentas if (c.sueldo_base or 0) > 0), cuentas[0])
        ingreso = principal.fecha_ingreso
        if ingreso and ingreso <= hoy:
            anios = hoy.year - ingreso.year - (1 if _aniversario(ingreso, hoy.year) > hoy else 0)
            ciclo_inicio = _aniversario(ingreso, ingreso.year + anios)
            ciclo_fin = _aniversario(ingreso, ingreso.year + anios + 1) - timedelta(days=1)
            corresponden = _dias_lft(anios)
            if _solo_fin_de_semana(principal.horario_semanal):
                corresponden = math.ceil(corresponden / 3)
            usados = sum(
                float(v.dias or 0)
                for v in aprobadas.get(clave, [])
                if ciclo_inicio <= v.fecha_inicio <= ciclo_fin
            )
            saldo.update(
                dias_corresponden=corresponden,
                ciclo_inicio=ciclo_inicio,
                ciclo_fin=ciclo_fin,
                dias_usados=usados,
                dias_quedan=corresponden - usados,
            )
        saldos[clave] = saldo
    return saldos


@router.post("", response_model=VacacionResponse, status_code=201)
@router.post("/", response_model=VacacionResponse, status_code=201, include_in_schema=False)
def crear_vacacion(
    body: VacacionCrear,
    db: Session = Depends(get_db),
    admin: models.Usuario = Depends(solo_admin),
):
    if body.fecha_fin < body.fecha_inicio:
        raise HTTPException(status_code=400, detail="fecha_fin no puede ser anterior a fecha_inicio")

    usuario = _usuario_activo(db, body.clave)
    if usuario is None:
        raise HTTPException(status_code=400, detail="La clave no corresponde a un usuario activo")

    clave_norm = _normalizar(body.clave)
    # Serializa las altas de una misma persona hasta el commit; sin esto, un
    # doble envio pasaria el chequeo de empalme dos veces.
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"vacaciones:{clave_norm}"))))

    empalme = (
        db.query(models.Vacacion)
        .filter(
            _clave_columna(models.Vacacion.clave) == clave_norm,
            models.Vacacion.estado.is_distinct_from("rechazada"),
            models.Vacacion.fecha_inicio <= body.fecha_fin,
            models.Vacacion.fecha_fin >= body.fecha_inicio,
        )
        .order_by(models.Vacacion.fecha_inicio)
        .first()
    )
    if empalme is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Se empalma con el periodo del {empalme.fecha_inicio} "
                f"al {empalme.fecha_fin} ({empalme.estado})"
            ),
        )

    vacacion = models.Vacacion(
        clave=usuario.nombre_englobado.strip(),
        fecha_inicio=body.fecha_inicio,
        fecha_fin=body.fecha_fin,
        dias=body.dias,
        tipo=body.tipo,
        estado="solicitada" if body.tipo == "vacaciones" else "autorizada",
        origen="sistema",
        nota=body.nota,
        creado_por=admin.id,
        creado_at=datetime.now(timezone.utc),
    )
    db.add(vacacion)
    db.commit()
    db.refresh(vacacion)
    return _serializar(vacacion, {})


@router.patch("/{vacacion_id}/aprobacion", response_model=VacacionResponse)
def registrar_aprobacion(
    vacacion_id: int,
    body: AprobacionBody,
    db: Session = Depends(get_db),
    admin: models.Usuario = Depends(solo_admin),
):
    vacacion = _obtener_vacacion(db, vacacion_id, bloquear=True)
    if vacacion.tipo != "vacaciones" or vacacion.estado != "solicitada":
        raise HTTPException(
            status_code=409,
            detail="Solo se pueden aprobar vacaciones en estado solicitada",
        )

    setattr(vacacion, f"aprobado_{body.nivel}", body.aprobado)
    setattr(vacacion, f"aprobado_{body.nivel}_por", admin.id)
    setattr(vacacion, f"aprobado_{body.nivel}_at", datetime.now(timezone.utc))
    if body.nivel == "gerencia":
        vacacion.estado = "autorizada" if body.aprobado else "rechazada"

    db.commit()
    db.refresh(vacacion)
    return _serializar(vacacion, _nombres_aprobadores(db, [vacacion]))


@router.patch("/{vacacion_id}/cubre", response_model=VacacionResponse)
def asignar_cubre(
    vacacion_id: int,
    body: CubreBody,
    db: Session = Depends(get_db),
    _admin: models.Usuario = Depends(solo_admin),
):
    vacacion = _obtener_vacacion(db, vacacion_id, bloquear=True)

    if body.cubre_clave is None:
        vacacion.cubre_clave = None
    else:
        usuario = _usuario_activo(db, body.cubre_clave)
        if usuario is None:
            raise HTTPException(status_code=400, detail="La clave no corresponde a un usuario activo")
        if _normalizar(body.cubre_clave) == _normalizar(vacacion.clave):
            raise HTTPException(status_code=400, detail="La persona no puede cubrirse a si misma")
        vacacion.cubre_clave = usuario.nombre_englobado.strip()

    db.commit()
    db.refresh(vacacion)
    return _serializar(vacacion, _nombres_aprobadores(db, [vacacion]))


@router.delete("/{vacacion_id}")
def eliminar_vacacion(
    vacacion_id: int,
    db: Session = Depends(get_db),
    _admin: models.Usuario = Depends(solo_admin),
):
    vacacion = _obtener_vacacion(db, vacacion_id, bloquear=True)
    if vacacion.origen != "sistema":
        raise HTTPException(status_code=409, detail="Solo se borran registros con origen sistema")

    db.delete(vacacion)
    db.commit()
    return {"ok": True}
