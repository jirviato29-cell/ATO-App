import calendar
import math
import unicodedata
from typing import Annotated, Dict, Iterable, List, Literal, Optional, Union
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app import avisos, cubre_por_dia, models
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


class CubreDiaResponse(BaseModel):
    fecha: date
    cubre_clave: Optional[str] = None  # None = "No tengo quién me cubra" ese día
    confirmado: Optional[bool] = None
    confirmado_por: Optional[str] = None
    confirmado_at: Optional[datetime] = None


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
    # Confirmación de quien cubre: null sin respuesta, true confirmó, false no puede
    cubre_confirmado: Optional[bool] = None
    cubre_confirmado_por: Optional[str] = None
    cubre_confirmado_at: Optional[datetime] = None
    # Quién cubre cada día (solo si eligió gente distinta por día; vacío = como siempre, cubre_clave)
    cubre_por_dia: List[CubreDiaResponse] = []


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
    # Ultimo dia para tomarlos: aniversario + 18 meses - 1 dia (null en el año 0)
    caduca: Optional[date] = None
    # Reparto FIFO: lo que salio de ESTE año, aunque se haya tomado en otra fecha
    dias_usados: float
    dias_solicitados_pendientes: float
    dias_quedan: float


class AsignacionPeriodo(BaseModel):
    id: int  # vacacion
    dias: float  # dias de ese periodo que salen de este año


class AnioVacaciones(SaldoAnio):
    dias_tomados: float
    dias_descuentos: float
    dias_adelanto: float  # parte de dias_tomados que se tomo antes de ganar el año
    vigente: bool  # ciclo_inicio <= hoy <= caduca
    caducado: bool
    periodos: List[AsignacionPeriodo] = []


class SaldoResponse(BaseModel):
    clave: str
    # null si la persona no tiene fecha de ingreso
    actual: Optional[SaldoAnio] = None
    siguiente: Optional[SaldoAnio] = None
    # Del año 1 al siguiente, con su caducidad y el reparto de cada periodo
    anios: List[AnioVacaciones] = []
    # Hoy, entre los años vigentes: lo que le queda y lo que aun puede pedir (sin lo solicitado)
    total_quedan: Optional[float] = None
    total_disponibles: Optional[float] = None


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
    caduca: Optional[date] = None
    dias_adelanto: float = 0
    # Solo ciclos cerrados y ya caducados (18 meses): lo que no se uso se pierde
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
    # Solo en solicitudes por día: cambia quién cubre ESA fecha
    fecha: Optional[date] = None


class ConfirmacionCubreBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmo: bool
    # Solo en solicitudes por día: confirma los días de esa persona
    cubre_clave: Optional[str] = Field(None, max_length=50)


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


def _nombres_aprobadores(
    db: Session,
    vacaciones: Iterable[models.Vacacion],
    dias: Optional[Dict[int, List[models.VacacionCubre]]] = None,
) -> Dict[int, str]:
    vacaciones = list(vacaciones)
    ids = {
        getattr(v, f"aprobado_{nivel}_por")
        for v in vacaciones
        for nivel in NIVELES
    } | {v.cubre_confirmado_por for v in vacaciones}
    ids |= {f.confirmado_por for filas in (dias or {}).values() for f in filas}
    ids -= {None}
    if not ids:
        return {}
    filas = (
        db.query(models.Usuario.id, models.Usuario.nombre_englobado, models.Usuario.username)
        .filter(models.Usuario.id.in_(ids))
        .all()
    )
    return {f.id: (f.nombre_englobado or "").strip() or f.username for f in filas}


def _serializar(v: models.Vacacion, nombres: Dict[int, str], dias: Optional[List[models.VacacionCubre]] = None) -> dict:
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
    datos["cubre_confirmado"] = v.cubre_confirmado
    datos["cubre_confirmado_por"] = nombres.get(v.cubre_confirmado_por) if v.cubre_confirmado_por is not None else None
    datos["cubre_confirmado_at"] = v.cubre_confirmado_at
    datos["cubre_por_dia"] = [
        {
            "fecha": f.fecha,
            "cubre_clave": (f.cubre_clave or "").strip() or None,
            "confirmado": f.confirmado,
            "confirmado_por": nombres.get(f.confirmado_por) if f.confirmado_por is not None else None,
            "confirmado_at": f.confirmado_at,
        }
        for f in (dias or [])
    ]
    return datos


def serializar_con_dias(db: Session, vacaciones: Iterable[models.Vacacion]) -> List[dict]:
    """_serializar de varias solicitudes con sus días de quién cubre (1 consulta para todas)."""
    vacaciones = list(vacaciones)
    dias = cubre_por_dia.dias_de(db, [v.id for v in vacaciones])
    nombres = _nombres_aprobadores(db, vacaciones, dias)
    return [_serializar(v, nombres, dias.get(v.id)) for v in vacaciones]


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
    return serializar_con_dias(db, vacaciones)


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

    return [
        {
            **datos,
            "nombre_completo": nombre_por_clave.get(_normalizar(v.clave)),
            **saldos.get(v.id, _SALDO_VACIO),
        }
        for v, datos in zip(vacaciones, serializar_con_dias(db, vacaciones))
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


# Reforma "vacaciones dignas": la tabla nueva aplica a aniversarios desde el 01/01/2023
REFORMA_LFT = date(2023, 1, 1)


def _dias_lft(anios: int, aniversario: date) -> int:
    """Dias que tocan por LFT segun la tabla vigente en la FECHA DE ANIVERSARIO del ciclo.

    Unica funcion para los dias de un ciclo (saldo, solicitudes y panel).
    - Desde 01/01/2023: 1=12, 2=14, 3=16, 4=18, 5=20; de ahi +2 cada 5 anios (6-10=22, 11-15=24, 16-20=26, ...)
    - Antes:            1=6, 2=8, 3=10, 4=12; 5-9=14, 10-14=16, 15-19=18, 20-24=20, 25-29=22 (+2 cada 5 anios)
    """
    if anios <= 0:
        return 0
    if aniversario >= REFORMA_LFT:
        if anios <= 5:
            return 10 + 2 * anios
        return 20 + 2 * math.ceil((anios - 5) / 5)
    if anios <= 4:
        return 4 + 2 * anios
    return 14 + 2 * ((anios - 5) // 5)


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


# Vigencia de cada año de vacaciones (LFT arts. 81 y 516): del aniversario al
# aniversario + 18 meses - 1 dia. Despues caduca. Con 18 meses nunca hay mas de
# 2 años vigentes a la vez (el atrasado y el actual).
MESES_VIGENCIA = 18
_CERO = 1e-9


def _caducidad(aniversario: date) -> date:
    # Si el dia no existe en ese mes (p. ej. 31 -> febrero), se toma el ultimo del mes
    meses = aniversario.month - 1 + MESES_VIGENCIA
    anio = aniversario.year + meses // 12
    mes = meses % 12 + 1
    dia = min(aniversario.day, calendar.monthrange(anio, mes)[1])
    return date(anio, mes, dia) - timedelta(days=1)


def _anios_cumplidos(ingreso: date, fecha: date) -> int:
    # Antes de la fecha de ingreso cuenta como año 0
    return max(0, fecha.year - ingreso.year - (1 if _aniversario(ingreso, fecha.year) > fecha else 0))


def _estado(v: models.Vacacion) -> str:
    return (v.estado or "").strip().lower()


class _Reparto:
    """Reparte los dias usados entre los años de vacaciones (FIFO).

    - Cada dia de vacaciones (autorizada/tomada) sale del año MAS VIEJO que ya se gano,
      no ha caducado y tiene dias, en la fecha_inicio del periodo.
    - Si no hay ninguno, es adelanto: sale del siguiente año que se va a ganar (con dias).
    - Un descuento sale del año que empieza en su fecha (se fecha en el aniversario).
    - Las solicitadas se reparten igual, despues de todo lo firme (no le quitan dias a lo firme).
    """

    def __init__(self, principal: models.Usuario, periodos: List[models.Vacacion], nuevo=None):
        self.ingreso: date = principal.fecha_ingreso
        self.fin_de_semana = _solo_fin_de_semana(principal.horario_semanal)
        self.anios: Dict[int, dict] = {}
        self.adelanto_total = 0.0
        firmes = [
            v for v in periodos
            if v.tipo == "descuento" or (v.tipo == "vacaciones" and _estado(v) in ("autorizada", "tomada"))
        ]
        pendientes = [v for v in periodos if v.tipo == "vacaciones" and _estado(v) == "solicitada"]

        def orden(v):
            return (v.fecha_inicio, v.id or 0)

        for v in sorted(firmes, key=orden):
            if v.tipo == "descuento":
                self._descuento(v)
            else:
                self._consumir(float(v.dias or 0), v.fecha_inicio, v.id, pendiente=False)
        for v in sorted(pendientes, key=orden):
            self._consumir(float(v.dias or 0), v.fecha_inicio, v.id, pendiente=True)
        if nuevo is not None:
            # Solicitud que se esta validando (aun sin id): (fecha_inicio, dias)
            self._consumir(float(nuevo[1]), nuevo[0], None, pendiente=True)

    def anio(self, n: int) -> dict:
        if n not in self.anios:
            inicio = _aniversario(self.ingreso, self.ingreso.year + n)
            corresponden = _dias_lft(n, inicio)
            if self.fin_de_semana:
                corresponden = math.ceil(corresponden / 3)
            self.anios[n] = {
                "ciclo_inicio": inicio,
                "ciclo_fin": _aniversario(self.ingreso, self.ingreso.year + n + 1) - timedelta(days=1),
                "caduca": _caducidad(inicio) if n > 0 else None,
                "dias_corresponden": corresponden,
                "dias_tomados": 0.0,
                "dias_descuentos": 0.0,
                "dias_adelanto": 0.0,
                "dias_solicitados_pendientes": 0.0,
                "restante": float(corresponden),  # tras lo firme y lo solicitado
                "periodos": {},  # id de vacacion -> dias que salen de este año
            }
        return self.anios[n]

    def _descuento(self, v: models.Vacacion) -> None:
        a = self.anio(max(1, _anios_cumplidos(self.ingreso, v.fecha_inicio)))
        dias = float(v.dias or 0)
        a["dias_descuentos"] += dias
        a["restante"] -= dias
        a["periodos"][v.id] = a["periodos"].get(v.id, 0.0) + dias

    def vigentes(self, fecha: date) -> List[dict]:
        # Años ganados y sin caducar en esa fecha, el mas viejo primero (a lo mas 2)
        k = _anios_cumplidos(self.ingreso, fecha)
        return [
            self.anio(n) for n in (k - 1, k)
            if n >= 1 and self.anio(n)["ciclo_inicio"] <= fecha <= self.anio(n)["caduca"]
        ]

    def _consumir(self, dias: float, fecha: date, vid, pendiente: bool) -> None:
        falta = dias
        while falta > _CERO:
            adelanto = False
            a = next((x for x in self.vigentes(fecha) if x["restante"] > _CERO), None)
            if a is None:
                # Adelanto: del siguiente año que se va a ganar y que todavia tiene dias
                adelanto = True
                n = _anios_cumplidos(self.ingreso, fecha) + 1
                while self.anio(n)["restante"] <= _CERO:
                    n += 1
                a = self.anio(n)
            t = min(falta, a["restante"])
            a["restante"] -= t
            falta -= t
            if pendiente:
                a["dias_solicitados_pendientes"] += t
            else:
                a["dias_tomados"] += t
                if adelanto:
                    a["dias_adelanto"] += t
            if adelanto:
                self.adelanto_total += t
            if vid is not None:
                a["periodos"][vid] = a["periodos"].get(vid, 0.0) + t

    def saldo(self, n: int) -> dict:
        a = self.anio(n)
        usados = a["dias_tomados"] + a["dias_descuentos"]
        return {
            "anios": n,
            "dias_corresponden": a["dias_corresponden"],
            "ciclo_inicio": a["ciclo_inicio"],
            "ciclo_fin": a["ciclo_fin"],
            "caduca": a["caduca"],
            "dias_tomados": a["dias_tomados"],
            "dias_descuentos": a["dias_descuentos"],
            "dias_adelanto": a["dias_adelanto"],
            "dias_usados": usados,
            "dias_solicitados_pendientes": a["dias_solicitados_pendientes"],
            "dias_quedan": a["dias_corresponden"] - usados,
        }

    def disponibles(self, fecha: date) -> float:
        # Lo que todavia se puede pedir para esa fecha: años vigentes, sin lo solicitado
        return sum(max(a["restante"], 0.0) for a in self.vigentes(fecha))


def _reparto(principal: Optional[models.Usuario], periodos: List[models.Vacacion], nuevo=None) -> Optional[_Reparto]:
    if not principal or not principal.fecha_ingreso:
        return None
    return _Reparto(principal, periodos, nuevo)


def _calcular_saldo(
    principal: Optional[models.Usuario],
    periodos: List[models.Vacacion],
    fecha: date,
    reparto: Optional[_Reparto] = None,
) -> Optional[dict]:
    """Saldo del año de vacaciones donde cae `fecha`. None si no hay fecha de ingreso.

    Los numeros salen del reparto FIFO (_Reparto): usados = lo que salio de ESTE año
    (aunque el periodo sea de otra fecha), pendientes = solicitadas que saldrian de este año.
    """
    reparto = reparto or _reparto(principal, periodos)
    if reparto is None:
        return None
    return reparto.saldo(_anios_cumplidos(reparto.ingreso, fecha))


def anios_de_vacaciones(reparto: _Reparto, hoy: date) -> List[dict]:
    """Del año 1 al siguiente al actual (mas los que tengan adelantos), con caducidad y
    los dias que salen de cada periodo."""
    actual = _anios_cumplidos(reparto.ingreso, hoy)
    hasta = max([actual + 1] + [n for n, a in reparto.anios.items() if a["periodos"]])
    filas = []
    for n in range(1, hasta + 1):
        a = reparto.anio(n)
        filas.append(
            {
                **reparto.saldo(n),
                "vigente": a["ciclo_inicio"] <= hoy <= a["caduca"],
                "caducado": hoy > a["caduca"],
                "periodos": [{"id": vid, "dias": d} for vid, d in a["periodos"].items()],
            }
        )
    return filas


def _datos_persona(db: Session, clave_norm: str):
    principal = _principal(_cuentas_por_clave(db, [clave_norm]).get(clave_norm, []))
    return principal, _periodos_saldo(db, [clave_norm]).get(clave_norm, [])


def saldo_vacaciones(db: Session, clave: str, fecha: date) -> Optional[dict]:
    """Funcion unica de saldo por (clave, fecha)."""
    principal, periodos = _datos_persona(db, _normalizar(clave))
    return _calcular_saldo(principal, periodos, fecha)


def saldo_actual_y_siguiente(db: Session, clave: str) -> dict:
    clave_norm = _normalizar(clave)
    hoy = datetime.now(ZONA_MX).date()
    principal, periodos = _datos_persona(db, clave_norm)
    reparto = _reparto(principal, periodos)
    if reparto is None:
        return {"clave": clave_norm, "actual": None, "siguiente": None}
    actual = _calcular_saldo(principal, periodos, hoy, reparto)
    siguiente = _calcular_saldo(principal, periodos, actual["ciclo_fin"] + timedelta(days=1), reparto)
    anios = anios_de_vacaciones(reparto, hoy)
    vigentes = [a for a in anios if a["vigente"]]
    return {
        "clave": clave_norm,
        "actual": actual,
        "siguiente": siguiente,
        "anios": anios,
        # Hoy, entre los años vigentes (el atrasado y el actual)
        "total_quedan": sum(max(a["dias_quedan"], 0.0) for a in vigentes),
        "total_disponibles": reparto.disponibles(hoy),
    }


def _saldos_solicitudes(db: Session, vacaciones: List[models.Vacacion]) -> Dict[int, dict]:
    # Por solicitud: modulos de la persona y saldo del año donde cae su fecha_inicio
    claves = {_normalizar(v.clave) for v in vacaciones}
    cuentas = _cuentas_por_clave(db, claves)
    periodos = _periodos_saldo(db, claves)
    repartos: Dict[str, Optional[_Reparto]] = {}
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
        principal = _principal(cuentas[clave])
        if clave not in repartos:
            repartos[clave] = _reparto(principal, periodos.get(clave, []))
        reparto = repartos[clave]
        saldo = _calcular_saldo(principal, periodos.get(clave, []), v.fecha_inicio, reparto)
        if saldo:
            datos.update({k: saldo[k] for k in _SALDO_VACIO if k in saldo})
            # "Le quedan" = lo de los años vigentes en esa fecha (atrasado + actual)
            vig = reparto.vigentes(v.fecha_inicio)
            datos["dias_quedan"] = sum(
                a["dias_corresponden"] - a["dias_tomados"] - a["dias_descuentos"] for a in vig
            )
            datos["dias_solicitados_pendientes"] = sum(a["dias_solicitados_pendientes"] for a in vig)
        resultado[v.id] = datos
    return resultado


def _ciclos_persona(reparto: _Reparto, actual: dict, hoy: date) -> List[dict]:
    # Del primer aniversario al ciclo actual, mas el siguiente (con 0 años, el actual y el siguiente)
    ciclos: List[dict] = []
    for anios in range(min(1, actual["anios"]), actual["anios"] + 2):
        saldo = reparto.saldo(anios)
        if anios < actual["anios"]:
            estado = "Cerrado"
        elif anios == actual["anios"]:
            estado = "Actual"
        else:
            estado = "Siguiente"
        caducado = saldo["caduca"] is not None and hoy > saldo["caduca"]
        ciclos.append(
            {
                **saldo,
                "estado": estado,
                # Lo que sobro se pierde solo cuando el año ya caduco (18 meses)
                "dias_perdidos": max(saldo["dias_quedan"], 0.0) if estado == "Cerrado" and caducado else None,
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
        reparto = _reparto(principal, periodos.get(clave, []))
        actual = _calcular_saldo(principal, periodos.get(clave, []), hoy, reparto)
        if actual:
            fila.update(
                {k: actual[k] for k in (
                    "anios", "dias_corresponden", "dias_tomados", "dias_descuentos",
                    "dias_solicitados_pendientes", "dias_quedan", "ciclo_inicio", "ciclo_fin",
                )},
                dias_para_aniversario=(actual["ciclo_fin"] + timedelta(days=1) - hoy).days,
                ciclos=_ciclos_persona(reparto, actual, hoy),
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
        raise HTTPException(status_code=400, detail=f"Máximo {MAX_DIAS_SEGUIDOS} días por solicitud. Si necesitas más días, habla con Gerencia.")
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
    """Sin adelantos al pedir: los dias deben salir de años ganados y sin caducar en
    fecha_inicio (el atrasado primero), sin dejar en adelanto nada de lo ya pedido.
    Llamar despues del candado de la persona (cuenta las solicitudes pendientes)."""
    principal, periodos = _datos_persona(db, _normalizar(clave))
    base = _reparto(principal, periodos)
    if base is None:
        raise HTTPException(
            status_code=400,
            detail=f"No {'tienes' if tu else 'tiene'} fecha de ingreso; pide a RH que la registre",
        )
    if _anios_cumplidos(base.ingreso, inicio) == 0:
        raise HTTPException(
            status_code=400,
            detail="Aún no cumples tu primer año" if tu else "Aún no cumple su primer año",
        )
    con_nuevo = _Reparto(principal, periodos, nuevo=(inicio, dias))
    if con_nuevo.adelanto_total <= base.adelanto_total + _CERO:
        return
    disponibles = base.disponibles(inicio)
    raise HTTPException(
        status_code=400,
        detail=(
            f"Solo {'te' if tu else 'le'} quedan {_numero(max(disponibles, 0))} días "
            f"para tomar a partir del {_texto_fecha(inicio)}"
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

# Empleado: el día extra se pide con 7 días de anticipación (primer día = hoy + 7),
# cualquier día de la semana, y nunca del 15 de diciembre al 6 de enero (cada año).
DIAS_ANTICIPACION_EXTRA = 7


def dia_extra_bloqueado(d: date) -> bool:
    return (d.month == 12 and d.day >= 15) or (d.month == 1 and d.day <= 6)


# Tipos que se suman para el máximo de días seguidos
TIPOS_SEGUIDOS = ("vacaciones", "dia_extra")


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


def validar_fechas_dia_extra_empleado(inicio: date, fin: date, hoy: date) -> None:
    """Empleado: 7 días de anticipación, de lunes a domingo, sin 15 dic - 6 ene.
    La vigencia (festivo a vencimiento) la revisa validar_dias_extra."""
    primer_dia = hoy + timedelta(days=DIAS_ANTICIPACION_EXTRA)
    if inicio < primer_dia:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Los días extra se piden con {DIAS_ANTICIPACION_EXTRA} días de anticipación: "
                f"puedes pedir a partir del {_texto_fecha(primer_dia)}"
            ),
        )
    for d in _rango(inicio, fin):
        if dia_extra_bloqueado(d):
            raise HTTPException(
                status_code=400,
                detail=f"El {_texto_fecha(d)} está bloqueado (15 dic - 6 ene): no se pueden pedir días extra",
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
    background_tasks: BackgroundTasks,
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
    # Aviso al empleado (solo si tiene MENSAJES), después del commit: nunca rompe el otorgamiento
    background_tasks.add_task(avisos.dia_extra_otorgado, clave_norm, body.fecha_festivo, body.dias)
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
        # Admin: cualquier fecha dentro de la vigencia (sin anticipación ni bloqueos), días enteros
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


def es_pendiente_con_aprobacion(v: models.Vacacion) -> bool:
    return v.tipo in TIPOS_CON_APROBACION and (v.estado or "").strip().lower() == "solicitada"


def autorizar_si_completa(v: models.Vacacion, dias: Optional[List[models.VacacionCubre]] = None) -> bool:
    """Pasa a 'autorizada' solo con los 4 en verde: RH, Encargado, Gerencia y quien cubre
    confirmó. Devuelve True si la autorizó en esta llamada. Llamar con la fila bloqueada.
    Con días por persona (dias), quien cubre está en verde solo si TODOS los días tienen
    persona y todas confirmaron; sin días, la regla de siempre (cubre_clave confirmado)."""
    if dias:
        cubre_ok = cubre_por_dia.completo(dias)
    else:
        cubre_ok = bool(_normalizar(v.cubre_clave)) and v.cubre_confirmado is True
    completa = (
        es_pendiente_con_aprobacion(v)
        and v.aprobado_rh is True
        and v.aprobado_encargado is True
        and v.aprobado_gerencia is True
        and cubre_ok
    )
    if completa:
        v.estado = "autorizada"
    return completa


def registrar_respuesta_cubre(v: models.Vacacion, confirmo: bool, usuario_id: int) -> None:
    v.cubre_confirmado = confirmo
    v.cubre_confirmado_por = usuario_id
    v.cubre_confirmado_at = datetime.now(timezone.utc)


def aplicar_aprobacion(
    db: Session,
    vacacion: models.Vacacion,
    nivel: str,
    aprobado: bool,
    usuario: models.Usuario,
    background_tasks: BackgroundTasks,
) -> None:
    """Marca un nivel (RH / Encargado / Gerencia), autoriza o rechaza si corresponde y
    programa los avisos. La usan admin (PATCH /vacaciones/{id}/aprobacion) y quienes
    revisan (PATCH /aprobaciones/{id}); el permiso se revisa ANTES de llamarla.
    Llamar con la fila bloqueada (_obtener_vacacion(..., bloquear=True))."""
    if vacacion.tipo not in TIPOS_CON_APROBACION or vacacion.estado != "solicitada":
        raise HTTPException(
            status_code=409,
            detail="Solo se pueden aprobar vacaciones en estado solicitada",
        )

    encargado_antes = vacacion.aprobado_encargado
    setattr(vacacion, f"aprobado_{nivel}", aprobado)
    setattr(vacacion, f"aprobado_{nivel}_por", usuario.id)
    setattr(vacacion, f"aprobado_{nivel}_at", datetime.now(timezone.utc))
    rechazada = nivel == "gerencia" and not aprobado
    if rechazada:
        # Gerencia rechaza: se rechaza de inmediato, como siempre
        vacacion.estado = "rechazada"
    # Se autoriza solo con los 4 en verde (RH, Encargado, Gerencia y quien cubre confirmó)
    autorizada = autorizar_si_completa(vacacion, cubre_por_dia.dias_de_una(db, vacacion.id))

    db.commit()
    db.refresh(vacacion)
    # Avisos después del commit y fuera de la petición: nunca rompen la aprobación
    if rechazada:
        background_tasks.add_task(
            avisos.decision_gerencia, vacacion.clave, vacacion.tipo, vacacion.fecha_inicio,
            vacacion.fecha_fin, vacacion.dias, False, vacacion.id,
        )
    if nivel == "encargado" and aprobado and encargado_antes is not True:
        background_tasks.add_task(
            avisos.encargado_aprobo, vacacion.clave, vacacion.fecha_inicio, vacacion.fecha_fin, vacacion.id
        )
    if autorizada:
        background_tasks.add_task(
            avisos.solicitud_aprobada, vacacion.clave, vacacion.tipo, vacacion.fecha_inicio, vacacion.fecha_fin, vacacion.id
        )


@router.patch("/{vacacion_id}/aprobacion", response_model=VacacionResponse)
def registrar_aprobacion(
    vacacion_id: int,
    body: AprobacionBody,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: models.Usuario = Depends(solo_admin),
):
    vacacion = _obtener_vacacion(db, vacacion_id, bloquear=True)
    aplicar_aprobacion(db, vacacion, body.nivel, body.aprobado, admin, background_tasks)
    return serializar_con_dias(db, [vacacion])[0]


@router.patch("/{vacacion_id}/cubre", response_model=VacacionResponse)
def asignar_cubre(
    vacacion_id: int,
    body: CubreBody,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _admin: models.Usuario = Depends(solo_admin),
):
    vacacion = _obtener_vacacion(db, vacacion_id, bloquear=True)
    dias = cubre_por_dia.dias_de_una(db, vacacion.id)
    if body.fecha is not None:
        return _asignar_cubre_dia(db, vacacion, dias, body, background_tasks)

    nuevo_usuario = None
    if body.cubre_clave is not None:
        nuevo_usuario = _usuario_activo(db, body.cubre_clave)
        if nuevo_usuario is None:
            raise HTTPException(status_code=400, detail="La clave no corresponde a un usuario activo")
        if _normalizar(body.cubre_clave) == _normalizar(vacacion.clave):
            raise HTTPException(status_code=400, detail="La persona no puede cubrirse a si misma")

    if dias:
        # Tenía quién cubre por día y admin elige UNA persona para todos: vuelve al modo de siempre
        if not es_pendiente_con_aprobacion(vacacion):
            raise HTTPException(status_code=409, detail="La solicitud ya no está pendiente")
        confirmados = cubre_por_dia.por_persona(f for f in dias if f.confirmado is True)
        cubre_por_dia.borrar(db, vacacion.id)
        vacacion.cubre_clave = nuevo_usuario.nombre_englobado.strip() if nuevo_usuario else None
        vacacion.cubre_confirmado = None
        vacacion.cubre_confirmado_por = None
        vacacion.cubre_confirmado_at = None
        db.commit()
        db.refresh(vacacion)
        nuevo = _normalizar(vacacion.cubre_clave)
        if nuevo:
            background_tasks.add_task(
                avisos.cubrir_solicitud, nuevo, vacacion.clave, vacacion.fecha_inicio, vacacion.fecha_fin, vacacion.id
            )
        for clave, fechas in confirmados.items():
            if clave != nuevo:
                background_tasks.add_task(avisos.cobertura_cancelada_dias, clave, vacacion.clave, fechas, vacacion.id)
        return serializar_con_dias(db, [vacacion])[0]

    anterior = _normalizar(vacacion.cubre_clave)
    confirmado_antes = vacacion.cubre_confirmado
    vacacion.cubre_clave = nuevo_usuario.nombre_englobado.strip() if nuevo_usuario else None

    nuevo = _normalizar(vacacion.cubre_clave)
    # Pendiente y cambió la persona: se reinicia la confirmación y se avisa a la nueva.
    # En una ya autorizada (o tomada/rechazada) solo cambia el nombre, como antes.
    cambio = es_pendiente_con_aprobacion(vacacion) and nuevo != anterior
    if cambio:
        vacacion.cubre_confirmado = None
        vacacion.cubre_confirmado_por = None
        vacacion.cubre_confirmado_at = None

    db.commit()
    db.refresh(vacacion)
    if cambio:
        fechas = (vacacion.clave, vacacion.fecha_inicio, vacacion.fecha_fin, vacacion.id)
        if nuevo:
            background_tasks.add_task(avisos.cubrir_solicitud, nuevo, *fechas)
        if anterior and confirmado_antes is True:
            # Ya había confirmado: que sepa que ya no cubre
            background_tasks.add_task(avisos.cobertura_cancelada, anterior, *fechas)
    return serializar_con_dias(db, [vacacion])[0]


def _asignar_cubre_dia(
    db: Session,
    vacacion: models.Vacacion,
    dias: List[models.VacacionCubre],
    body: CubreBody,
    background_tasks: BackgroundTasks,
):
    # Solo admin reasigna un día (p. ej. cuando alguien dijo "no puedo"); la fila ya está bloqueada
    if not dias:
        raise HTTPException(status_code=409, detail="Esta solicitud no tiene quién cubre por día")
    if not es_pendiente_con_aprobacion(vacacion):
        raise HTTPException(status_code=409, detail="La solicitud ya no está pendiente")
    fila = next((f for f in dias if f.fecha == body.fecha), None)
    if fila is None:
        raise HTTPException(status_code=400, detail="Esa fecha no es parte de la solicitud")

    nuevo_usuario = None
    if _normalizar(body.cubre_clave):
        nuevo_usuario = _usuario_activo(db, body.cubre_clave)
        if nuevo_usuario is None:
            raise HTTPException(status_code=400, detail="La clave no corresponde a un usuario activo")
        if _normalizar(body.cubre_clave) == _normalizar(vacacion.clave):
            raise HTTPException(status_code=400, detail="La persona no puede cubrirse a si misma")

    anterior = _normalizar(fila.cubre_clave)
    confirmado_antes = fila.confirmado
    nuevo = _normalizar(nuevo_usuario.nombre_englobado) if nuevo_usuario else ""
    cambio = nuevo != anterior
    if cambio:
        fila.cubre_clave = nuevo_usuario.nombre_englobado.strip() if nuevo_usuario else None
        fila.confirmado = None
        fila.confirmado_por = None
        fila.confirmado_at = None
        cubre_por_dia.sincronizar(vacacion, dias)
    db.commit()
    db.refresh(vacacion)
    if cambio:
        if nuevo:
            background_tasks.add_task(avisos.cubrir_dias, nuevo, vacacion.clave, [body.fecha], vacacion.id)
        if anterior and confirmado_antes is True:
            background_tasks.add_task(avisos.cobertura_cancelada_dias, anterior, vacacion.clave, [body.fecha], vacacion.id)
    return serializar_con_dias(db, [vacacion])[0]


@router.post("/{vacacion_id}/cubre/recordatorio")
def recordar_cubre(
    vacacion_id: int,
    background_tasks: BackgroundTasks,
    cubre_clave: Optional[str] = Query(None, max_length=50),
    db: Session = Depends(get_db),
    _admin: models.Usuario = Depends(solo_admin),
):
    # Vuelve a mandar "¿Confirmas que vas a cubrir...?" (sirve para las solicitudes de antes del flujo)
    vacacion = _obtener_vacacion(db, vacacion_id)
    dias = cubre_por_dia.dias_de_una(db, vacacion.id)
    if dias:
        # Por día: a una persona, con sus días que siguen sin respuesta
        cubre = _normalizar(cubre_clave)
        if not cubre:
            raise HTTPException(status_code=400, detail="Indica a quién reenviar el aviso (cubre_clave)")
        mias = cubre_por_dia.de_persona(dias, cubre)
        if not es_pendiente_con_aprobacion(vacacion) or not mias:
            raise HTTPException(status_code=409, detail="La solicitud no está pendiente o esa persona no cubre ningún día")
        pendientes = [f.fecha for f in mias if f.confirmado is None]
        if not pendientes:
            raise HTTPException(status_code=409, detail="Quien cubre ya respondió")
        if cubre not in avisos.claves_con_mensajes(db):
            raise HTTPException(
                status_code=409,
                detail=f"{cubre} todavía no tiene MENSAJES: usa Confirmar por él si ya te confirmó",
            )
        background_tasks.add_task(avisos.cubrir_dias, cubre, vacacion.clave, pendientes, vacacion.id)
        return {"ok": True}

    cubre = _normalizar(vacacion.cubre_clave)
    if not es_pendiente_con_aprobacion(vacacion) or not cubre:
        raise HTTPException(status_code=409, detail="La solicitud no está pendiente o no tiene quién cubre")
    if vacacion.cubre_confirmado is not None:
        raise HTTPException(status_code=409, detail="Quien cubre ya respondió")
    if cubre not in avisos.claves_con_mensajes(db):
        raise HTTPException(
            status_code=409,
            detail=f"{cubre} todavía no tiene MENSAJES: usa Confirmar por él si ya te confirmó",
        )
    background_tasks.add_task(
        avisos.cubrir_solicitud, cubre, vacacion.clave, vacacion.fecha_inicio, vacacion.fecha_fin, vacacion.id
    )
    return {"ok": True}


@router.patch("/{vacacion_id}/cubre/confirmacion", response_model=VacacionResponse)
def confirmar_cubre_por_admin(
    vacacion_id: int,
    body: ConfirmacionCubreBody,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: models.Usuario = Depends(solo_admin),
):
    # "Confirmar por él": quien cubre avisó por otro medio (teléfono, en persona)
    vacacion = _obtener_vacacion(db, vacacion_id, bloquear=True)
    dias = cubre_por_dia.dias_de_una(db, vacacion.id)
    if dias:
        if not _normalizar(body.cubre_clave):
            raise HTTPException(status_code=400, detail="Indica de quién es la confirmación (cubre_clave)")
        mias = cubre_por_dia.de_persona(dias, body.cubre_clave)
        if not es_pendiente_con_aprobacion(vacacion) or not mias:
            raise HTTPException(status_code=409, detail="La solicitud no está pendiente o esa persona no cubre ningún día")
        cubre_por_dia.responder(mias, body.confirmo, admin.id)
        cubre_por_dia.sincronizar(vacacion, dias)
    else:
        if not es_pendiente_con_aprobacion(vacacion) or not _normalizar(vacacion.cubre_clave):
            raise HTTPException(status_code=409, detail="La solicitud no está pendiente o no tiene quién cubre")
        registrar_respuesta_cubre(vacacion, body.confirmo, admin.id)
    autorizada = autorizar_si_completa(vacacion, dias)
    db.commit()
    db.refresh(vacacion)
    if autorizada:
        background_tasks.add_task(
            avisos.solicitud_aprobada, vacacion.clave, vacacion.tipo, vacacion.fecha_inicio, vacacion.fecha_fin, vacacion.id
        )
    return serializar_con_dias(db, [vacacion])[0]


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
