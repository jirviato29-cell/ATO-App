import os
from datetime import date, datetime, timedelta, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import avisos, models
from app.config import get_current_user
from app.database import get_db
from app.permisos_vacaciones import mapa_encargados
from app.routers.vacaciones import (
    ESTADOS_VIGENTES,
    ZONA_MX,
    DiasExtraResponse,
    SaldoResponse,
    VacacionResponse,
    _clave_columna,
    _cuentas_por_clave,
    _nombres_aprobadores,
    _normalizar,
    _principal,
    _serializar,
    _texto_fecha,
    _usuario_activo,
    candado_persona,
    dias_extra_de,
    festivos_de,
    periodos_guardados,
    saldo_actual_y_siguiente,
    validar_contra_periodos,
    validar_dias_extra,
    validar_fechas_dia_extra_empleado,
    validar_fechas_vacaciones,
    validar_saldo,
    validar_tope_seguidos,
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


@router.get("/vacaciones/saldo", response_model=SaldoResponse)
def mi_saldo(
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(get_current_user),
):
    # Saldo del año de vacaciones actual y del siguiente; sin englobado no hay saldo
    englobado = _normalizar(usuario.nombre_englobado)
    if not englobado:
        return {"clave": _clave_de(usuario), "actual": None, "siguiente": None}
    return saldo_actual_y_siguiente(db, englobado)


# ------------------- Solicitudes de vacaciones (piloto) -------------------
# Con VACACIONES_PARA_TODOS apagado, solo los usernames de esta lista pueden pedir o cancelar vacaciones.
PILOTO_MI_PERFIL = ("A55BO-EVELYN",)


# Interruptor en Render (variable de entorno VACACIONES_PARA_TODOS=true): abre Mis vacaciones
# a todos los empleados (asesor/encargado activos). Apagado por default: solo el piloto.
# Se lee en cada llamada, igual que MENSAJES_PARA_TODOS (app/avisos.py).
def vacaciones_para_todos() -> bool:
    return os.getenv("VACACIONES_PARA_TODOS", "").strip().lower() in ("1", "true", "si", "sí", "yes")

# Igual que el calendario del frontend: primer dia elegible = hoy (Mexico) + 31.
# Las demas reglas (maximo de dias, bloqueos, uno por mes, 15 dias, saldo) son las
# mismas que POST /vacaciones de admin y viven en app/routers/vacaciones.py.
DIAS_ANTICIPACION = 31
# El empleado puede cancelar sus vacaciones hasta 7 días antes de que empiecen
DIAS_PARA_CANCELAR = 7


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
    # "dia_extra": día de descanso extra (no sale del saldo de vacaciones)
    tipo: Literal["vacaciones", "dia_extra"] = "vacaciones"


def _es_piloto(usuario: models.Usuario) -> bool:
    return _normalizar(usuario.username) in {_normalizar(u) for u in PILOTO_MI_PERFIL}


def tiene_mis_vacaciones(usuario: models.Usuario) -> bool:
    if _es_piloto(usuario):
        return True
    return vacaciones_para_todos() and bool(usuario.activo) and usuario.rol in ROLES_EMPLEADO


def _exigir_piloto(usuario: models.Usuario) -> None:
    if not tiene_mis_vacaciones(usuario):
        raise HTTPException(status_code=403, detail="Esta función todavía no está disponible para tu usuario")


class AccesoResponse(BaseModel):
    mis_vacaciones: bool


@router.get("/acceso", response_model=AccesoResponse)
def mi_acceso(usuario: models.Usuario = Depends(get_current_user)):
    # El frontend pregunta aquí si muestra "MIS VACACIONES": así manda el interruptor de Render
    return {"mis_vacaciones": tiene_mis_vacaciones(usuario)}


@router.get("/dias-extra", response_model=DiasExtraResponse)
def mis_dias_extra(
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(get_current_user),
):
    # Días de descanso extra de la propia persona; sin englobado no hay
    englobado = (usuario.nombre_englobado or "").strip()
    if not englobado:
        return {"clave": "", "dias_disponibles": 0, "vence": None, "dias_usados": 0, "dias_pendientes": 0, "otorgados": []}
    return dias_extra_de(db, englobado)


# ------------------- ¿Quién te cubre? (lista para elegir) -------------------
# Por persona (nombre_englobado) y su cuenta PRINCIPAL, igual que saldo y permisos:
# - Módulo con encargado: la gente de TODOS los módulos de su(s) encargado(s)
#   (ej. M1 -> A48-ZURY tiene M1 y VL -> salen los de M1 y VL). Cadenas C. tiene a
#   A57-ELI por asignación manual, así que salen solo los promotores de Cadenas C.
# - Módulo sin encargado (BO es del admin): la gente de ese mismo módulo.
# - Sin módulo: lista vacía (solo "No tengo quién me cubra" en el frontend).
# Nunca la propia persona, ni inactivos, ni quien tenga vacaciones o día extra
# (solicitada, autorizada o tomada) que se encime con esas fechas.
TIPOS_QUE_OCUPAN = ("vacaciones", "dia_extra")


class CubreOpcion(BaseModel):
    clave: str
    nombre: Optional[str] = None


def _modulos_para_cubrir(db: Session, modulo: Optional[int]) -> set:
    if modulo is None:
        return set()
    modulos = set()
    for de_encargado in mapa_encargados(db).values():
        if modulo in de_encargado:
            modulos |= de_encargado
    return modulos or {modulo}


def opciones_cubre(db: Session, clave_norm: str, inicio: date, fin: date) -> List[dict]:
    clave_norm = _normalizar(clave_norm)
    if not clave_norm:
        return []
    mia = _principal(_cuentas_por_clave(db, [clave_norm]).get(clave_norm, []))
    modulos = _modulos_para_cubrir(db, mia.modulo_id if mia is not None else None)
    if not modulos:
        return []

    # Candidatas: alguna cuenta activa de empleado en esos módulos; luego se revisa su principal
    claves = {
        _normalizar(f[0])
        for f in db.query(models.Usuario.nombre_englobado)
        .filter(
            models.Usuario.activo == True,  # noqa: E712
            models.Usuario.rol.in_(ROLES_EMPLEADO),
            models.Usuario.modulo_id.in_(modulos),
        )
        .all()
    }
    claves.discard("")
    claves.discard(clave_norm)
    if not claves:
        return []

    ocupadas = {
        _normalizar(v.clave)
        for v in db.query(models.Vacacion)
        .filter(
            _clave_columna(models.Vacacion.clave).in_(claves),
            models.Vacacion.tipo.in_(TIPOS_QUE_OCUPAN),
            models.Vacacion.fecha_inicio <= fin,
            models.Vacacion.fecha_fin >= inicio,
        )
        .all()
        if (v.estado or "").strip().lower() in ESTADOS_VIGENTES
    }

    opciones = []
    for clave, cuentas in _cuentas_por_clave(db, claves - ocupadas).items():
        principal = _principal(cuentas)
        if principal is None or principal.rol not in ROLES_EMPLEADO or principal.modulo_id not in modulos:
            continue
        nombre = next((c.nombre_completo.strip() for c in cuentas if (c.nombre_completo or "").strip()), None)
        opciones.append({"clave": (principal.nombre_englobado or "").strip() or clave, "nombre": nombre})
    return sorted(opciones, key=lambda o: _normalizar(o["clave"]))


@router.get("/cubre-opciones", response_model=List[CubreOpcion])
def cubre_opciones(
    inicio: date,
    fin: date,
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(get_current_user),
):
    _exigir_piloto(usuario)
    if fin < inicio:
        raise HTTPException(status_code=400, detail="La fecha final no puede ser anterior a la fecha de inicio")
    return opciones_cubre(db, _normalizar(usuario.nombre_englobado), inicio, fin)


@router.post("/vacaciones", response_model=VacacionResponse, status_code=201)
def pedir_vacaciones(
    body: SolicitudVacacion,
    background_tasks: BackgroundTasks,
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
    hoy = datetime.now(ZONA_MX).date()
    es_extra = body.tipo == "dia_extra"
    if es_extra:
        # Ventana especial del festivo (sin anticipación) o 20 días; bloqueos siempre
        validar_fechas_dia_extra_empleado(inicio, fin, hoy, festivos_de(db, clave_norm))
    else:
        primer_dia = hoy + timedelta(days=DIAS_ANTICIPACION)
        if inicio < primer_dia:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Las vacaciones se piden con {DIAS_ANTICIPACION} días de anticipación: "
                    f"puedes empezar a partir del {_texto_fecha(primer_dia)}"
                ),
            )

        validar_fechas_vacaciones(inicio, fin, tu=True)

    cubre = None
    if (body.cubre_clave or "").strip():
        # Solo alguien de la lista de GET /mi-perfil/cubre-opciones para estas mismas fechas
        permitidas = {_normalizar(o["clave"]) for o in opciones_cubre(db, clave_norm, inicio, fin)}
        cubre = _usuario_activo(db, body.cubre_clave) if _normalizar(body.cubre_clave) in permitidas else None
        if cubre is None:
            raise HTTPException(status_code=400, detail="Esa persona no puede cubrirte")

    # Mismo candado que POST /vacaciones: sin esto, un doble envio pasaria las validaciones dos veces.
    candado_persona(db, clave_norm)
    existentes = periodos_guardados(db, clave_norm)

    # Los descuentos son una fila fechada en el aniversario: no son un periodo y no se enciman
    for v in existentes:
        if v.tipo != "descuento" and v.fecha_inicio <= fin and v.fecha_fin >= inicio:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Se encima con tu periodo del {_texto_fecha(v.fecha_inicio)} "
                    f"al {_texto_fecha(v.fecha_fin)} ({v.estado})"
                ),
            )

    if es_extra:
        # Máximo 6 seguidos junto con vacaciones; que alcancen los días extra
        validar_tope_seguidos(existentes, inicio, fin, tu=True)
        validar_dias_extra(db, clave_norm, existentes, inicio, fin, tu=True)
    else:
        validar_contra_periodos(existentes, inicio, fin, tu=True)
        validar_tope_seguidos(existentes, inicio, fin, tu=True)
        # Sin adelantos: contra el año de vacaciones donde cae fecha_inicio
        validar_saldo(db, clave_norm, inicio, dias, tu=True)

    nota = (body.nota or "").strip() or None
    vacacion = models.Vacacion(
        clave=clave,
        fecha_inicio=inicio,
        fecha_fin=fin,
        dias=dias,
        estado="solicitada",
        tipo=body.tipo,
        origen="mi_perfil",
        nota=nota,
        creado_por=usuario.id,
        creado_at=datetime.now(timezone.utc),
        cubre_clave=cubre.nombre_englobado.strip() if cubre else None,
    )
    db.add(vacacion)
    db.commit()
    db.refresh(vacacion)
    # Aviso a administración, después del commit y fuera de la petición: nunca rompe la solicitud
    background_tasks.add_task(avisos.solicitud_nueva, clave, body.tipo, inicio, fin, dias, vacacion.id)
    if vacacion.cubre_clave:
        # Eligió quién la cubre: se le pide confirmar (CONFIRMO / NO PUEDO en su aviso)
        background_tasks.add_task(avisos.cubrir_solicitud, vacacion.cubre_clave, clave, inicio, fin, vacacion.id)
    return _serializar(vacacion, {})


@router.delete("/vacaciones/{vacacion_id}")
def cancelar_solicitud(
    vacacion_id: int,
    background_tasks: BackgroundTasks,
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
    # Regla: vacaciones solicitadas o autorizadas (aunque ya tengan visto bueno de RH, Encargado
    # o Gerencia) se cancelan hasta 7 días antes de fecha_inicio, con "hoy" en hora de México.
    # Ej.: inicio 11/01/2027 -> se puede cancelar hasta el 04/01/2027 inclusive.
    hoy = datetime.now(ZONA_MX).date()
    cancelable = (
        vacacion.tipo in ("vacaciones", "dia_extra")
        and (vacacion.estado or "").strip().lower() in ("solicitada", "autorizada")
        and (vacacion.fecha_inicio - hoy).days >= DIAS_PARA_CANCELAR
    )
    if not cancelable:
        raise HTTPException(
            status_code=400,
            detail=f"Ya no se puede cancelar: faltan menos de {DIAS_PARA_CANCELAR} días. Habla con RH.",
        )

    # Datos del aviso ANTES de borrar la fila
    datos_aviso = (vacacion.clave, vacacion.tipo, vacacion.fecha_inicio, vacacion.fecha_fin, vacacion.dias, vacacion.id)
    # Quien la iba a cubrir (salvo que ya hubiera dicho que no puede) recibe "Ya no tienes que cubrir..."
    cubre = vacacion.cubre_clave if vacacion.cubre_confirmado is not False else None
    db.delete(vacacion)
    db.commit()
    background_tasks.add_task(avisos.solicitud_cancelada, *datos_aviso)
    if cubre:
        background_tasks.add_task(
            avisos.cobertura_cancelada, cubre, datos_aviso[0], datos_aviso[2], datos_aviso[3], datos_aviso[5]
        )
    return {"ok": True}
