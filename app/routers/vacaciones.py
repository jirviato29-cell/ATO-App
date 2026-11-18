import math
import unicodedata
from typing import Annotated, Dict, Iterable, List, Literal, Optional, Union
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app import models
from app.database import get_db
from app.utilidades import verificar_rol_requerido

router = APIRouter()

# La tabla vacaciones vive en la BD y se altera a mano: aqui no hay migraciones.
# La clave de cada periodo es el nombre_englobado de la persona y siempre se
# compara con UPPER(TRIM()), porque los datos cargados de Excel no son uniformes.

NIVELES = ("rh", "encargado", "gerencia")
# Lo que pasa por RH, Encargado y Gerencia
TIPOS_CON_APROBACION = ("vacaciones", "dia_extra")

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
    tipo: Literal["vacaciones", "falta_injustificada", "dia_extra"]
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


class DiaExtraCrear(BaseModel):
    # Otorgar días de descanso extra (admin). extra="forbid": vence lo calcula la BD.
    model_config = ConfigDict(extra="forbid")

    clave: str = Field(..., min_length=1, max_length=50)
    fecha_festivo: date
    dias: int = Field(..., ge=1, le=6)
    motivo: str = Field(..., min_length=1, max_length=500)


class DiaExtraOtorgado(BaseModel):
    id: int
    clave: str
    fecha_festivo: date
    vence: date
    dias: int
    motivo: str
    creado_at: Optional[datetime] = None
    dias_asignados: int  # días pedidos o tomados que salen de este otorgamiento
    dias_restantes: int
    vigente: bool  # hoy <= vence


class DiasExtraResponse(BaseModel):
    clave: str
    # Lo que todavía puede pedir: otorgados vigentes menos lo ya pedido o tomado
    dias_disponibles: int
    # Vencimiento más cercano de los días disponibles (null si no hay)
    vence: Optional[date] = None
    dias_usados: int  # autorizados o tomados
    dias_pendientes: int  # solicitados
    otorgados: List[DiaExtraOtorgado] = []


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
            models.Vacacion.tipo.in_(TIPOS_CON_APROBACION),
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
        datos = {**_SALDO_VACIO, "modulos": modulos}
        # Un día extra no sale del saldo de vacaciones: sin columnas de saldo
        if v.tipo == "dia_extra":
            resultado[v.id] = datos
            continue
        saldo = _calcular_saldo(_principal(cuentas[clave]), periodos.get(clave, []), v.fecha_inicio)
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
# Ni las faltas, ni los descuentos, ni los días extra cuentan para "uno por mes" ni para los 15 días
TIPOS_SIN_REGLAS = ("falta_injustificada", "descuento", "dia_extra")

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


# ------------------- Días de descanso extra -------------------
# Se otorgan en la tabla dias_extra (vence = festivo + 1 año - 1 día) y se piden
# como filas de vacaciones con tipo 'dia_extra'. No salen del saldo de vacaciones
# ni cuentan para "uno por mes" o los 15 días; sí para empalme y para el máximo
# de 6 días seguidos junto con vacaciones.

# Empleado fuera de una ventana especial: 20 días de anticipación. Mismo criterio
# que vacaciones ("30 días" = primer día elegible hoy + 31): primer día = hoy + 21.
DIAS_ANTICIPACION_EXTRA = 21

# Ventanas especiales por festivo: dentro de ellas el empleado pide sin anticipación
# (desde mañana). Para otro festivo, agregar otra entrada.
VENTANAS_DIA_EXTRA = (
    {
        "fecha_festivo": date(2026, 11, 16),
        "inicio": date(2026, 11, 19),
        "fin": date(2026, 12, 4),
        "solo_entre_semana": True,
    },
)

# Tipos que se suman para el máximo de días seguidos
TIPOS_SEGUIDOS = ("vacaciones", "dia_extra")

_DIAS_SEMANA = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def _rango(inicio: date, fin: date) -> List[date]:
    return [inicio + timedelta(days=n) for n in range((fin - inicio).days + 1)]


def validar_dias_enteros(inicio: date, fin: date, dias: float) -> None:
    if fin < inicio:
        raise HTTPException(status_code=400, detail="fecha_fin no puede ser anterior a fecha_inicio")
    total = (fin - inicio).days + 1
    if float(dias) != float(total):
        raise HTTPException(
            status_code=400,
            detail=f"Los días extra van enteros: del {_texto_fecha(inicio)} al {_texto_fecha(fin)} son {total}",
        )


def validar_tope_seguidos(existentes: List[models.Vacacion], inicio: date, fin: date, tu: bool) -> None:
    """Máximo de días seguidos contando vacaciones y días extra pegados (no rechazados)."""
    ocupados = set(_rango(inicio, fin))
    for v in existentes:
        if v.tipo in TIPOS_SEGUIDOS and (v.estado or "").strip().lower() in ESTADOS_VIGENTES:
            ocupados.update(_rango(v.fecha_inicio, v.fecha_fin))
    desde, hasta = inicio, fin
    while desde - timedelta(days=1) in ocupados:
        desde -= timedelta(days=1)
    while hasta + timedelta(days=1) in ocupados:
        hasta += timedelta(days=1)
    seguidos = (hasta - desde).days + 1
    if seguidos > MAX_DIAS_SEGUIDOS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Máximo {MAX_DIAS_SEGUIDOS} días seguidos contando vacaciones y días extra: "
                f"del {_texto_fecha(desde)} al {_texto_fecha(hasta)} serían {seguidos}"
            ),
        )


def _otorgados(db: Session, claves: Optional[Iterable[str]] = None) -> Dict[str, List[models.DiaExtra]]:
    """Otorgamientos por clave. Sin claves: los de todas las personas (panel)."""
    query = db.query(models.DiaExtra)
    if claves is not None:
        claves = {c for c in claves if c}
        if not claves:
            return {}
        query = query.filter(_clave_columna(models.DiaExtra.clave).in_(claves))
    resultado: Dict[str, List[models.DiaExtra]] = {}
    for o in query.order_by(models.DiaExtra.vence, models.DiaExtra.id).all():
        resultado.setdefault(_normalizar(o.clave), []).append(o)
    return resultado


def _usos_dia_extra(periodos: Iterable[models.Vacacion]) -> List[models.Vacacion]:
    return [
        v for v in periodos
        if v.tipo == "dia_extra" and (v.estado or "").strip().lower() in ESTADOS_VIGENTES
    ]


def _dias_de(periodos: Iterable[models.Vacacion]) -> List[date]:
    return [d for v in periodos for d in _rango(v.fecha_inicio, v.fecha_fin)]


def _asignar(otorgados: List[models.DiaExtra], dias: Iterable[date]):
    """Cada día pedido sale del otorgamiento vigente para ese día que vence primero.
    Regresa (días asignados por id de otorgamiento, días que no alcanzaron)."""
    asignados = {o.id: 0 for o in otorgados}
    ordenados = sorted(otorgados, key=lambda o: (o.vence, o.id))
    sin_cubrir: List[date] = []
    for d in sorted(dias):
        o = next(
            (o for o in ordenados if o.fecha_festivo <= d <= o.vence and asignados[o.id] < int(o.dias)),
            None,
        )
        if o is None:
            sin_cubrir.append(d)
        else:
            asignados[o.id] += 1
    return asignados, sin_cubrir


def resumen_dias_extra(otorgados: List[models.DiaExtra], periodos: Iterable[models.Vacacion], hoy: date) -> dict:
    usos = _usos_dia_extra(periodos)
    asignados, _ = _asignar(otorgados, _dias_de(usos))
    detalle = []
    disponibles = 0
    vence = None
    for o in otorgados:
        restantes = max(int(o.dias) - asignados.get(o.id, 0), 0)
        vigente = o.vence >= hoy
        if vigente and restantes > 0:
            disponibles += restantes
            vence = o.vence if vence is None else min(vence, o.vence)
        detalle.append(
            {
                "id": o.id,
                "clave": o.clave,
                "fecha_festivo": o.fecha_festivo,
                "vence": o.vence,
                "dias": int(o.dias),
                "motivo": o.motivo,
                "creado_at": o.creado_at,
                "dias_asignados": asignados.get(o.id, 0),
                "dias_restantes": restantes,
                "vigente": vigente,
            }
        )
    estado = lambda v: (v.estado or "").strip().lower()  # noqa: E731
    return {
        "dias_disponibles": disponibles,
        "vence": vence,
        "dias_usados": int(sum(float(v.dias or 0) for v in usos if estado(v) in ("autorizada", "tomada"))),
        "dias_pendientes": int(sum(float(v.dias or 0) for v in usos if estado(v) == "solicitada")),
        "otorgados": detalle,
    }


def dias_extra_de(db: Session, clave: str) -> dict:
    clave_norm = _normalizar(clave)
    hoy = datetime.now(ZONA_MX).date()
    resumen = resumen_dias_extra(
        _otorgados(db, [clave_norm]).get(clave_norm, []), periodos_guardados(db, clave_norm), hoy
    )
    return {"clave": clave_norm, **resumen}


def festivos_de(db: Session, clave_norm: str) -> List[date]:
    return [o.fecha_festivo for o in _otorgados(db, [clave_norm]).get(clave_norm, [])]


def validar_dias_extra(
    db: Session, clave_norm: str, existentes: List[models.Vacacion], inicio: date, fin: date, tu: bool
) -> None:
    """Que alcancen los días extra: cada día debe caer entre un festivo y su vencimiento,
    en un otorgamiento con días sin usar. Llamar después del candado de la persona."""
    otorgados = _otorgados(db, [clave_norm]).get(clave_norm, [])
    if not otorgados:
        raise HTTPException(status_code=400, detail=f"No {'tienes' if tu else 'tiene'} días de descanso extra")
    previos = _dias_de(_usos_dia_extra(existentes))
    _, sin_cubrir = _asignar(otorgados, previos + _rango(inicio, fin))
    if not sin_cubrir:
        return
    d = sin_cubrir[0]
    if not any(o.fecha_festivo <= d <= o.vence for o in otorgados):
        raise HTTPException(
            status_code=400,
            detail=(
                f"El {_texto_fecha(d)} no está dentro de la vigencia de {'tus' if tu else 'sus'} días extra "
                f"(del festivo a su vencimiento)"
            ),
        )
    asignados, _ = _asignar(otorgados, previos)
    quedan = sum(max(int(o.dias) - asignados[o.id], 0) for o in otorgados if o.vence >= inicio)
    raise HTTPException(
        status_code=400,
        detail=(
            f"No {'te' if tu else 'le'} alcanzan los días extra: "
            f"{'te' if tu else 'le'} {'queda' if quedan == 1 else 'quedan'} {quedan}"
        ),
    )


def ventana_dia_extra(d: date, festivos: Iterable[date]) -> Optional[dict]:
    festivos = set(festivos)
    return next(
        (v for v in VENTANAS_DIA_EXTRA if v["fecha_festivo"] in festivos and v["inicio"] <= d <= v["fin"]),
        None,
    )


def validar_fechas_dia_extra_empleado(inicio: date, fin: date, hoy: date, festivos: Iterable[date]) -> None:
    """Empleado: dentro de una ventana especial de su festivo, sin anticipación (desde mañana)
    y, si la ventana lo pide, solo lunes a viernes; fuera de ella, 20 días de anticipación.
    Las fechas bloqueadas aplican siempre."""
    festivos = list(festivos)
    primer_dia = hoy + timedelta(days=DIAS_ANTICIPACION_EXTRA)
    for d in _rango(inicio, fin):
        if _dia_bloqueado(d):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"El {_texto_fecha(d)} está en periodo de ventas (7 dic - 7 ene o 13 - 16 nov): "
                    f"no se pueden pedir días extra"
                ),
            )
        ventana = ventana_dia_extra(d, festivos)
        if ventana:
            if d <= hoy:
                raise HTTPException(status_code=400, detail="Los días extra se piden a partir de mañana")
            if ventana["solo_entre_semana"] and d.weekday() >= 5:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Del {_texto_fecha(ventana['inicio'])} al {_texto_fecha(ventana['fin'])} el día extra "
                        f"solo se puede de lunes a viernes: el {_texto_fecha(d)} es {_DIAS_SEMANA[d.weekday()]}"
                    ),
                )
        elif d < primer_dia:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Los días extra se piden con 20 días de anticipación: "
                    f"puedes pedir a partir del {_texto_fecha(primer_dia)}"
                ),
            )


@router.get("/dias-extra", response_model=DiasExtraResponse)
def ver_dias_extra(
    clave: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    _admin: models.Usuario = Depends(solo_admin),
):
    return dias_extra_de(db, clave)


@router.get("/dias-extra/panel", response_model=List[DiasExtraResponse])
def panel_dias_extra(
    db: Session = Depends(get_db),
    _admin: models.Usuario = Depends(solo_admin),
):
    """Columna "Días extra" del Panel de control: una fila por persona con días otorgados.
    Va aparte de GET /panel para no cambiar esa respuesta. Dos consultas fijas."""
    hoy = datetime.now(ZONA_MX).date()
    otorgados = _otorgados(db)
    if not otorgados:
        return []
    periodos: Dict[str, List[models.Vacacion]] = {}
    for v in (
        db.query(models.Vacacion)
        .filter(
            _clave_columna(models.Vacacion.clave).in_(otorgados.keys()),
            models.Vacacion.tipo == "dia_extra",
        )
        .all()
    ):
        periodos.setdefault(_normalizar(v.clave), []).append(v)
    return [
        {"clave": clave, **resumen_dias_extra(lista, periodos.get(clave, []), hoy)}
        for clave, lista in sorted(otorgados.items())
    ]


@router.post("/dias-extra", response_model=DiasExtraResponse, status_code=201)
def otorgar_dias_extra(
    body: DiaExtraCrear,
    db: Session = Depends(get_db),
    admin: models.Usuario = Depends(solo_admin),
):
    usuario = _usuario_activo(db, body.clave)
    if usuario is None:
        raise HTTPException(status_code=400, detail="La clave no corresponde a un usuario activo")
    motivo = body.motivo.strip()
    if not motivo:
        raise HTTPException(status_code=400, detail="El motivo es obligatorio")
    if body.fecha_festivo > datetime.now(ZONA_MX).date():
        raise HTTPException(status_code=400, detail="La fecha del festivo no puede ser futura")
    clave_norm = _normalizar(body.clave)
    candado_persona(db, clave_norm)
    repetido = (
        db.query(models.DiaExtra)
        .filter(
            _clave_columna(models.DiaExtra.clave) == clave_norm,
            models.DiaExtra.fecha_festivo == body.fecha_festivo,
        )
        .first()
    )
    mensaje_repetido = f"Ya tiene días extra por el festivo del {_texto_fecha(body.fecha_festivo)}"
    if repetido:
        raise HTTPException(status_code=409, detail=mensaje_repetido)
    db.add(
        models.DiaExtra(
            clave=usuario.nombre_englobado.strip(),
            fecha_festivo=body.fecha_festivo,
            dias=body.dias,
            motivo=motivo,
            creado_por=admin.id,
            creado_at=datetime.now(timezone.utc),
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # Índice único (clave, festivo): doble envío al mismo tiempo
        db.rollback()
        raise HTTPException(status_code=409, detail=mensaje_repetido)
    return dias_extra_de(db, clave_norm)


@router.delete("/dias-extra/{otorgado_id}")
def quitar_dias_extra(
    otorgado_id: int,
    db: Session = Depends(get_db),
    _admin: models.Usuario = Depends(solo_admin),
):
    otorgado = db.query(models.DiaExtra).filter(models.DiaExtra.id == otorgado_id).first()
    if otorgado is None:
        raise HTTPException(status_code=404, detail="Otorgamiento no encontrado")
    clave_norm = _normalizar(otorgado.clave)
    candado_persona(db, clave_norm)
    resto = [o for o in _otorgados(db, [clave_norm]).get(clave_norm, []) if o.id != otorgado.id]
    _, sin_cubrir = _asignar(resto, _dias_de(_usos_dia_extra(periodos_guardados(db, clave_norm))))
    if sin_cubrir:
        raise HTTPException(
            status_code=409,
            detail="No se puede quitar: hay días extra pedidos o tomados que salen de este otorgamiento",
        )
    db.delete(otorgado)
    db.commit()
    return {"ok": True}


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
    es_extra = body.tipo == "dia_extra"
    if es_vacacion:
        validar_fechas_vacaciones(body.fecha_inicio, body.fecha_fin, tu=False)
    if es_extra:
        # Admin: cualquier fecha (sin anticipación, ventana ni bloqueos), días enteros
        validar_dias_enteros(body.fecha_inicio, body.fecha_fin, body.dias)

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
        validar_tope_seguidos(existentes, body.fecha_inicio, body.fecha_fin, tu=False)
        validar_saldo(db, clave_norm, body.fecha_inicio, body.dias, tu=False)
    if es_extra:
        validar_tope_seguidos(existentes, body.fecha_inicio, body.fecha_fin, tu=False)
        validar_dias_extra(db, clave_norm, existentes, body.fecha_inicio, body.fecha_fin, tu=False)

    if not es_vacacion and not es_extra:
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
    if vacacion.tipo not in TIPOS_CON_APROBACION or vacacion.estado != "solicitada":
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
