import math
import unicodedata
from typing import Annotated, Dict, Iterable, List, Literal, Optional, Union
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

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
    # Saldo del año de vacaciones donde cae fecha_inicio (aniversario a aniversario);
    # null sin fecha_ingreso
    dias_corresponden: Optional[int] = None
    ciclo_inicio: Optional[date] = None
    ciclo_fin: Optional[date] = None
    dias_usados: Optional[float] = None
    dias_solicitados_pendientes: Optional[float] = None
    dias_quedan: Optional[float] = None


class SaldoAnio(BaseModel):
    anios: int
    dias_corresponden: int
    ciclo_inicio: date
    ciclo_fin: date
    dias_usados: float
    dias_solicitados_pendientes: float
    dias_quedan: float


class SaldoResponse(BaseModel):
    clave: str
    # null si la persona no tiene fecha de ingreso
    actual: Optional[SaldoAnio] = None
    siguiente: Optional[SaldoAnio] = None


class CicloPanel(BaseModel):
    estado: Literal["Cerrado", "Actual", "Siguiente"]
    ciclo_inicio: date
    ciclo_fin: date
    anios: int
    dias_corresponden: int
    dias_tomados: float
    dias_descuentos: float
    dias_solicitados_pendientes: float
    dias_quedan: float
    # Solo ciclos cerrados: lo que no se uso se pierde (no se acumula)
    dias_perdidos: Optional[float] = None


class PersonaPanel(BaseModel):
    clave: str
    nombre_completo: Optional[str] = None
    modulos: List[str] = []
    fecha_ingreso: Optional[date] = None
    # Todo lo de abajo es null sin fecha de ingreso
    anios: Optional[int] = None
    dias_para_aniversario: Optional[int] = None
    dias_corresponden: Optional[int] = None
    dias_tomados: Optional[float] = None
    dias_descuentos: Optional[float] = None
    dias_solicitados_pendientes: Optional[float] = None
    dias_quedan: Optional[float] = None
    ciclo_inicio: Optional[date] = None
    ciclo_fin: Optional[date] = None
    ciclos: List[CicloPanel] = []


class VacacionCrear(BaseModel):
    clave: str = Field(..., min_length=1, max_length=50)
    fecha_inicio: date
    fecha_fin: date
    dias: float = Field(..., gt=0, le=999.9)
    tipo: Literal["vacaciones", "falta_injustificada"]
    nota: Optional[str] = None


class DescuentoCrear(BaseModel):
    # La fecha la calcula el servidor: si mandan fechas u otro campo, se rechaza (422)
    model_config = ConfigDict(extra="forbid")

    tipo: Literal["descuento"]
    clave: str = Field(..., min_length=1, max_length=50)
    anio: Literal["actual", "siguiente"]
    dias: float = Field(..., gt=0, le=999.9)
    nota: str = Field(..., min_length=1, max_length=500)


CrearBody = Annotated[Union[VacacionCrear, DescuentoCrear], Field(discriminator="tipo")]


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
    saldos = _saldos_solicitudes(db, vacaciones)

    nombres = _nombres_aprobadores(db, vacaciones)
    return [
        {
            **_serializar(v, nombres),
            "nombre_completo": nombre_por_clave.get(_normalizar(v.clave)),
            **saldos.get(v.id, _SALDO_VACIO),
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
    "dias_solicitados_pendientes": None,
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


def _cuentas_por_clave(db: Session, claves: Iterable[str]) -> Dict[str, List[models.Usuario]]:
    # Misma regla que Mis datos / Mi Perfil: cuentas activas de empleado con ese
    # nombre_englobado (si no tiene ninguna de empleado, sus cuentas activas).
    claves = {c for c in claves if c}
    if not claves:
        return {}
    todas: Dict[str, List[models.Usuario]] = {}
    for u in (
        db.query(models.Usuario)
        .filter(
            _clave_columna(models.Usuario.nombre_englobado).in_(claves),
            models.Usuario.activo == True,
        )
        .order_by(models.Usuario.id)
        .all()
    ):
        todas.setdefault(_normalizar(u.nombre_englobado), []).append(u)
    return {
        clave: [c for c in cuentas if c.rol in ROLES_EMPLEADO] or cuentas
        for clave, cuentas in todas.items()
    }


def _periodos_saldo(db: Session, claves: Iterable[str]) -> Dict[str, List[models.Vacacion]]:
    # Lo que cuenta para el saldo: vacaciones (el estado se revisa despues) y descuentos
    claves = {c for c in claves if c}
    if not claves:
        return {}
    periodos: Dict[str, List[models.Vacacion]] = {}
    for v in (
        db.query(models.Vacacion)
        .filter(
            _clave_columna(models.Vacacion.clave).in_(claves),
            models.Vacacion.tipo.in_(("vacaciones", "descuento")),
        )
        .all()
    ):
        periodos.setdefault(_normalizar(v.clave), []).append(v)
    return periodos


def _principal(cuentas: List[models.Usuario]) -> Optional[models.Usuario]:
    # La principal es la primera (por id) con sueldo > 0; si ninguna, la primera
    if not cuentas:
        return None
    return next((c for c in cuentas if (c.sueldo_base or 0) > 0), cuentas[0])


def _calcular_saldo(
    principal: Optional[models.Usuario], periodos: List[models.Vacacion], fecha: date
) -> Optional[dict]:
    """Saldo del año de vacaciones donde cae `fecha`. None si no hay fecha de ingreso.

    usados = vacaciones autorizadas/tomadas + descuentos, con fecha_inicio dentro del año.
    pendientes = vacaciones en 'solicitada' dentro del año (no se restan de quedan).
    Antes de la fecha de ingreso cuenta como año 0 (le tocan 0).
    """
    ingreso = principal.fecha_ingreso if principal else None
    if not ingreso:
        return None
    anios = max(0, fecha.year - ingreso.year - (1 if _aniversario(ingreso, fecha.year) > fecha else 0))
    ciclo_inicio = _aniversario(ingreso, ingreso.year + anios)
    ciclo_fin = _aniversario(ingreso, ingreso.year + anios + 1) - timedelta(days=1)
    corresponden = _dias_lft(anios)
    if _solo_fin_de_semana(principal.horario_semanal):
        corresponden = math.ceil(corresponden / 3)

    tomados = descuentos = pendientes = 0.0
    for v in periodos:
        # Cada periodo cuenta en el año donde empieza
        if not (ciclo_inicio <= v.fecha_inicio <= ciclo_fin):
            continue
        estado = (v.estado or "").strip().lower()
        if v.tipo == "descuento":
            descuentos += float(v.dias or 0)
        elif v.tipo == "vacaciones" and estado in ("autorizada", "tomada"):
            tomados += float(v.dias or 0)
        elif v.tipo == "vacaciones" and estado == "solicitada":
            pendientes += float(v.dias or 0)
    usados = tomados + descuentos
    return {
        "anios": anios,
        "dias_corresponden": corresponden,
        "ciclo_inicio": ciclo_inicio,
        "ciclo_fin": ciclo_fin,
        "dias_tomados": tomados,
        "dias_descuentos": descuentos,
        "dias_usados": usados,
        "dias_solicitados_pendientes": pendientes,
        "dias_quedan": corresponden - usados,
    }


def saldo_vacaciones(db: Session, clave: str, fecha: date) -> Optional[dict]:
    """Funcion unica de saldo por (clave, fecha)."""
    clave_norm = _normalizar(clave)
    principal = _principal(_cuentas_por_clave(db, [clave_norm]).get(clave_norm, []))
    return _calcular_saldo(principal, _periodos_saldo(db, [clave_norm]).get(clave_norm, []), fecha)


def saldo_actual_y_siguiente(db: Session, clave: str) -> dict:
    clave_norm = _normalizar(clave)
    hoy = datetime.now(ZONA_MX).date()
    actual = saldo_vacaciones(db, clave_norm, hoy)
    siguiente = (
        saldo_vacaciones(db, clave_norm, actual["ciclo_fin"] + timedelta(days=1)) if actual else None
    )
    return {"clave": clave_norm, "actual": actual, "siguiente": siguiente}


def _saldos_solicitudes(db: Session, vacaciones: List[models.Vacacion]) -> Dict[int, dict]:
    # Por solicitud: modulos de la persona y saldo del año donde cae su fecha_inicio
    claves = {_normalizar(v.clave) for v in vacaciones}
    cuentas = _cuentas_por_clave(db, claves)
    periodos = _periodos_saldo(db, claves)
    resultado: Dict[int, dict] = {}
    for v in vacaciones:
        clave = _normalizar(v.clave)
        if clave not in cuentas:
            resultado[v.id] = _SALDO_VACIO
            continue
        modulos: List[str] = []
        for c in cuentas[clave]:
            if c.modulo and c.modulo.nombre and c.modulo.nombre not in modulos:
                modulos.append(c.modulo.nombre)
        saldo = _calcular_saldo(_principal(cuentas[clave]), periodos.get(clave, []), v.fecha_inicio)
        datos = {**_SALDO_VACIO, "modulos": modulos}
        if saldo:
            datos.update({k: saldo[k] for k in _SALDO_VACIO if k in saldo})
        resultado[v.id] = datos
    return resultado


def _ciclos_persona(principal: models.Usuario, periodos: List[models.Vacacion], actual: dict) -> List[dict]:
    # Del primer aniversario al ciclo actual, mas el siguiente (con 0 años, el actual y el siguiente)
    ingreso = principal.fecha_ingreso
    ciclos: List[dict] = []
    for anios in range(min(1, actual["anios"]), actual["anios"] + 2):
        saldo = _calcular_saldo(principal, periodos, _aniversario(ingreso, ingreso.year + anios))
        if anios < actual["anios"]:
            estado = "Cerrado"
        elif anios == actual["anios"]:
            estado = "Actual"
        else:
            estado = "Siguiente"
        ciclos.append(
            {
                **saldo,
                "estado": estado,
                # Los dias no se acumulan: lo que sobro de un ciclo cerrado se pierde
                "dias_perdidos": max(saldo["dias_quedan"], 0.0) if estado == "Cerrado" else None,
            }
        )
    return ciclos


@router.get("/panel", response_model=List[PersonaPanel])
def panel_vacaciones(
    db: Session = Depends(get_db),
    _admin: models.Usuario = Depends(solo_admin),
):
    """Solo lectura: resumen del año de vacaciones actual y todos los ciclos de cada persona activa."""
    hoy = datetime.now(ZONA_MX).date()
    # Una persona = cuentas activas de empleado con el mismo nombre_englobado (o username sin englobado)
    # Consultas fijas: usuarios (con su modulo, joinedload) una vez y vacaciones una vez;
    # todos los ciclos se calculan en memoria.
    cuentas: Dict[str, List[models.Usuario]] = {}
    for u in (
        db.query(models.Usuario)
        .options(joinedload(models.Usuario.modulo))
        .filter(models.Usuario.activo == True, models.Usuario.rol.in_(ROLES_EMPLEADO))
        .order_by(models.Usuario.id)
        .all()
    ):
        clave = _normalizar(u.nombre_englobado) or _normalizar(u.username)
        if clave:
            cuentas.setdefault(clave, []).append(u)
    periodos = _periodos_saldo(db, cuentas.keys())

    filas = []
    for clave, lista in cuentas.items():
        principal = _principal(lista)
        modulos: List[str] = []
        for c in lista:
            if c.modulo and c.modulo.nombre and c.modulo.nombre not in modulos:
                modulos.append(c.modulo.nombre)
        fila = {
            "clave": clave,
            "nombre_completo": next(
                (c.nombre_completo.strip() for c in lista if (c.nombre_completo or "").strip()), None
            ),
            "modulos": modulos,
            "fecha_ingreso": principal.fecha_ingreso,
            "ciclos": [],
        }
        actual = _calcular_saldo(principal, periodos.get(clave, []), hoy)
        if actual:
            fila.update(
                {k: actual[k] for k in (
                    "anios", "dias_corresponden", "dias_tomados", "dias_descuentos",
                    "dias_solicitados_pendientes", "dias_quedan", "ciclo_inicio", "ciclo_fin",
                )},
                dias_para_aniversario=(actual["ciclo_fin"] + timedelta(days=1) - hoy).days,
                ciclos=_ciclos_persona(principal, periodos.get(clave, []), actual),
            )
        filas.append(fila)
    return sorted(filas, key=lambda f: f["clave"])


# ------------------- Reglas de periodos (admin y Mi Perfil) -------------------
MAX_DIAS_SEGUIDOS = 6
DIAS_ENTRE_PERIODOS = 15
# Periodos que ya cuentan para "uno por mes" y para la separacion de 15 dias
ESTADOS_VIGENTES = ("solicitada", "autorizada", "tomada")
# Ni las faltas ni los descuentos son periodos de vacaciones
TIPOS_SIN_REGLAS = ("falta_injustificada", "descuento")

MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def _texto_fecha(d: date) -> str:
    return f"{d.day} de {MESES[d.month - 1]} de {d.year}"


def _numero(n: float) -> str:
    return f"{n:g}"


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


def validar_fechas_vacaciones(inicio: date, fin: date, tu: bool) -> None:
    """Maximo de dias seguidos y dias bloqueados. `tu`: mensajes para la propia persona."""
    if (fin - inicio).days + 1 > MAX_DIAS_SEGUIDOS:
        raise HTTPException(status_code=400, detail=f"Máximo {MAX_DIAS_SEGUIDOS} días seguidos por periodo")
    for n in range((fin - inicio).days + 1):
        d = inicio + timedelta(days=n)
        if _dia_bloqueado(d):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"El {_texto_fecha(d)} está en periodo de ventas (7 dic - 7 ene o 13 - 16 nov): "
                    f"no se pueden {'pedir' if tu else 'registrar'} vacaciones"
                ),
            )


def validar_contra_periodos(existentes: List[models.Vacacion], inicio: date, fin: date, tu: bool) -> None:
    """Uno por mes y 15 dias entre periodos, contra los periodos ya guardados (no rechazados)."""
    vigentes = [
        v for v in existentes
        if (v.estado or "").strip().lower() in ESTADOS_VIGENTES and v.tipo not in TIPOS_SIN_REGLAS
    ]
    meses_nuevos = _meses(inicio, fin)
    for v in vigentes:
        if meses_nuevos & _meses(v.fecha_inicio, v.fecha_fin):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Solo se permite un periodo de vacaciones por mes: ya {'tienes' if tu else 'tiene'} el del "
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
                    f"{'tienes' if tu else 'tiene'} uno del {_texto_fecha(v.fecha_inicio)} al {_texto_fecha(v.fecha_fin)}"
                ),
            )


def validar_saldo(db: Session, clave: str, inicio: date, dias: float, tu: bool) -> None:
    """Sin adelantos: se valida contra el año de vacaciones donde cae fecha_inicio.
    Llamar despues del candado de la persona (cuenta las solicitudes pendientes)."""
    saldo = saldo_vacaciones(db, clave, inicio)
    if saldo is None:
        raise HTTPException(
            status_code=400,
            detail=f"No {'tienes' if tu else 'tiene'} fecha de ingreso; pide a RH que la registre",
        )
    if saldo["anios"] == 0:
        raise HTTPException(
            status_code=400,
            detail="Aún no cumples tu primer año" if tu else "Aún no cumple su primer año",
        )
    disponibles = saldo["dias_quedan"] - saldo["dias_solicitados_pendientes"]
    if dias > disponibles:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Solo {'te' if tu else 'le'} quedan {_numero(max(disponibles, 0))} días "
                f"en el año de vacaciones del {_texto_fecha(saldo['ciclo_inicio'])} "
                f"al {_texto_fecha(saldo['ciclo_fin'])}"
            ),
        )


def candado_persona(db: Session, clave_norm: str) -> None:
    # Serializa las altas de una misma persona hasta el commit; sin esto, un
    # doble envio pasaria las validaciones dos veces.
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"vacaciones:{clave_norm}"))))


def periodos_guardados(db: Session, clave_norm: str) -> List[models.Vacacion]:
    return (
        db.query(models.Vacacion)
        .filter(
            _clave_columna(models.Vacacion.clave) == clave_norm,
            models.Vacacion.estado.is_distinct_from("rechazada"),
        )
        .order_by(models.Vacacion.fecha_inicio)
        .all()
    )


@router.get("/saldo", response_model=SaldoResponse)
def ver_saldo(
    clave: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    _admin: models.Usuario = Depends(solo_admin),
):
    return saldo_actual_y_siguiente(db, clave)


@router.post("", response_model=VacacionResponse, status_code=201)
@router.post("/", response_model=VacacionResponse, status_code=201, include_in_schema=False)
def crear_vacacion(
    body: CrearBody,
    db: Session = Depends(get_db),
    admin: models.Usuario = Depends(solo_admin),
):
    usuario = _usuario_activo(db, body.clave)
    if usuario is None:
        raise HTTPException(status_code=400, detail="La clave no corresponde a un usuario activo")
    clave_norm = _normalizar(body.clave)

    if isinstance(body, DescuentoCrear):
        return _crear_descuento(db, body, usuario, admin)

    if body.fecha_fin < body.fecha_inicio:
        raise HTTPException(status_code=400, detail="fecha_fin no puede ser anterior a fecha_inicio")

    # Admin no tiene la regla de anticipacion (puede capturar fechas cercanas o
    # pasadas), pero si: maximo de dias, bloqueos, uno por mes, 15 dias y saldo.
    # Las faltas injustificadas siguen sin reglas.
    es_vacacion = body.tipo == "vacaciones"
    if es_vacacion:
        validar_fechas_vacaciones(body.fecha_inicio, body.fecha_fin, tu=False)

    candado_persona(db, clave_norm)
    existentes = periodos_guardados(db, clave_norm)

    # Los descuentos son una fila fechada en el aniversario: no son un periodo y no empalman
    empalme = next(
        (
            v for v in existentes
            if v.tipo != "descuento" and v.fecha_inicio <= body.fecha_fin and v.fecha_fin >= body.fecha_inicio
        ),
        None,
    )
    if empalme is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Se empalma con el periodo del {empalme.fecha_inicio} "
                f"al {empalme.fecha_fin} ({empalme.estado})"
            ),
        )

    if es_vacacion:
        validar_contra_periodos(existentes, body.fecha_inicio, body.fecha_fin, tu=False)
        validar_saldo(db, clave_norm, body.fecha_inicio, body.dias, tu=False)

    if not es_vacacion:
        estado = "autorizada"
    elif body.fecha_fin < datetime.now(ZONA_MX).date():
        # Vacaciones que ya pasaron: se capturan como tomadas
        estado = "tomada"
    else:
        estado = "solicitada"

    vacacion = models.Vacacion(
        clave=usuario.nombre_englobado.strip(),
        fecha_inicio=body.fecha_inicio,
        fecha_fin=body.fecha_fin,
        dias=body.dias,
        tipo=body.tipo,
        estado=estado,
        origen="sistema",
        nota=body.nota,
        creado_por=admin.id,
        creado_at=datetime.now(timezone.utc),
    )
    db.add(vacacion)
    db.commit()
    db.refresh(vacacion)
    return _serializar(vacacion, {})


def _crear_descuento(db: Session, body: DescuentoCrear, usuario: models.Usuario, admin: models.Usuario):
    nota = body.nota.strip()
    if not nota:
        raise HTTPException(status_code=400, detail="El motivo del descuento es obligatorio")

    # Un descuento es una fila fechada en el primer dia (aniversario) del año al que se descuenta
    saldo = saldo_actual_y_siguiente(db, body.clave)[body.anio]
    if saldo is None:
        raise HTTPException(status_code=400, detail="No tiene fecha de ingreso; pide a RH que la registre")
    fecha = saldo["ciclo_inicio"]

    vacacion = models.Vacacion(
        clave=usuario.nombre_englobado.strip(),
        fecha_inicio=fecha,
        fecha_fin=fecha,
        dias=body.dias,
        tipo="descuento",
        estado="autorizada",
        origen="ajuste",
        nota=nota,
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
    # sistema = capturado por admin; ajuste = descuento de dias
    if vacacion.origen not in ("sistema", "ajuste"):
        raise HTTPException(status_code=409, detail="Solo se borran registros con origen sistema o ajuste")

    db.delete(vacacion)
    db.commit()
    return {"ok": True}
