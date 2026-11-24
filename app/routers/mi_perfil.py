import os
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import avisos, cubre_por_dia, models
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
    serializar_con_dias,
    _usuario_activo,
    candado_persona,
    dias_extra_de,
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
    return serializar_con_dias(db, vacaciones)


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


class CubreDia(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fecha: date
    # null = "No tengo quién me cubra" ese día
    cubre_clave: Optional[str] = Field(None, max_length=50)


class SolicitudVacacion(BaseModel):
    # extra="forbid": si mandan clave, id o cualquier otro campo, se rechaza (422)
    model_config = ConfigDict(extra="forbid")

    fecha_inicio: date
    fecha_fin: date
    # Opcional: el servidor lo calcula; si viene y no coincide, se rechaza
    dias: Optional[int] = None
    # Opcional: si no lo pone la persona, admin lo asigna con PATCH /vacaciones/{id}/cubre
    cubre_clave: Optional[str] = Field(None, max_length=50)
    # La persona eligió "No tengo quién me cubra". El día extra exige cubre_clave o esto.
    sin_cubre: bool = False
    # "Cubrir por día": una fila por CADA fecha del periodo. Si todas quedan con la misma
    # persona se guarda como siempre (cubre_clave), sin filas por día.
    cubre_por_dia: Optional[List[CubreDia]] = Field(None, max_length=62)
    nota: Optional[str] = Field(None, max_length=500)
    # "dia_extra": día de descanso extra (no sale del saldo de vacaciones)
    tipo: Literal["vacaciones", "dia_extra"] = "vacaciones"


def _es_piloto(usuario: models.Usuario) -> bool:
    return _normalizar(usuario.username) in {_normalizar(u) for u in PILOTO_MI_PERFIL}


# Interruptor por grupo en Render (VACACIONES_GRUPOS=cadenas): abre Mis vacaciones solo a
# la gente de esos grupos aunque VACACIONES_PARA_TODOS esté apagada. Un grupo = módulos por
# nombre; cuenta el módulo de la cuenta PRINCIPAL de la persona (misma regla que saldo,
# permisos y "quién te cubre"). Varios grupos separados por coma. Se lee en cada llamada.
GRUPOS_VACACIONES = {"cadenas": ("Cadenas C.",)}


def modulos_de_grupos_vacaciones() -> set:
    nombres = set()
    for g in os.getenv("VACACIONES_GRUPOS", "").split(","):
        nombres.update(GRUPOS_VACACIONES.get(g.strip().lower(), ()))
    return nombres


def en_grupo_vacaciones(db: Session, usuario: models.Usuario) -> bool:
    nombres = modulos_de_grupos_vacaciones()
    if not nombres:
        return False
    englobado = _normalizar(usuario.nombre_englobado)
    cuentas = _cuentas_por_clave(db, [englobado]).get(englobado, []) if englobado else []
    principal = _principal(cuentas) or usuario
    return principal.modulo is not None and principal.modulo.nombre in nombres


def claves_en_grupos_vacaciones(db: Session) -> set:
    """Claves de todas las personas activas de los grupos encendidos (para MENSAJES y avisos)."""
    from app.permisos_vacaciones import personas_activas

    nombres = modulos_de_grupos_vacaciones()
    if not nombres:
        return set()
    return {c for c, p in personas_activas(db).items() if p.modulo in nombres}


def tiene_mis_vacaciones(usuario: models.Usuario, db: Session) -> bool:
    if _es_piloto(usuario):
        return True
    if not (bool(usuario.activo) and usuario.rol in ROLES_EMPLEADO):
        return False
    return vacaciones_para_todos() or en_grupo_vacaciones(db, usuario)


def _exigir_piloto(usuario: models.Usuario, db: Session) -> None:
    if not tiene_mis_vacaciones(usuario, db):
        raise HTTPException(status_code=403, detail="Esta función todavía no está disponible para tu usuario")


class AccesoResponse(BaseModel):
    mis_vacaciones: bool


@router.get("/acceso", response_model=AccesoResponse)
def mi_acceso(
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(get_current_user),
):
    # El frontend pregunta aquí si muestra "MIS VACACIONES": así mandan los interruptores de Render
    return {"mis_vacaciones": tiene_mis_vacaciones(usuario, db)}


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


def _candidatas_cubre(db: Session, clave_norm: str):
    """(claves candidatas, módulos) de quien puede cubrir a clave_norm, sin revisar fechas."""
    clave_norm = _normalizar(clave_norm)
    if not clave_norm:
        return set(), set()
    mia = _principal(_cuentas_por_clave(db, [clave_norm]).get(clave_norm, []))
    modulos = _modulos_para_cubrir(db, mia.modulo_id if mia is not None else None)
    if not modulos:
        return set(), set()

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
    return claves, modulos


def _ocupaciones(db: Session, claves: set, inicio: date, fin: date) -> List[tuple]:
    """(clave, inicio, fin) de vacaciones o día extra vigentes de esas claves que tocan el rango."""
    return [
        (_normalizar(v.clave), v.fecha_inicio, v.fecha_fin)
        for v in db.query(models.Vacacion)
        .filter(
            _clave_columna(models.Vacacion.clave).in_(claves),
            models.Vacacion.tipo.in_(TIPOS_QUE_OCUPAN),
            models.Vacacion.fecha_inicio <= fin,
            models.Vacacion.fecha_fin >= inicio,
        )
        .all()
        if (v.estado or "").strip().lower() in ESTADOS_VIGENTES
    ]


def _armar_opciones(db: Session, claves: set, modulos: set) -> List[dict]:
    opciones = []
    for clave, cuentas in _cuentas_por_clave(db, claves).items():
        principal = _principal(cuentas)
        if principal is None or principal.rol not in ROLES_EMPLEADO or principal.modulo_id not in modulos:
            continue
        nombre = next((c.nombre_completo.strip() for c in cuentas if (c.nombre_completo or "").strip()), None)
        opciones.append({"clave": (principal.nombre_englobado or "").strip() or clave, "nombre": nombre})
    return sorted(opciones, key=lambda o: _normalizar(o["clave"]))


def opciones_cubre(db: Session, clave_norm: str, inicio: date, fin: date) -> List[dict]:
    claves, modulos = _candidatas_cubre(db, clave_norm)
    if not claves:
        return []
    ocupadas = {c for c, _, _ in _ocupaciones(db, claves, inicio, fin)}
    return _armar_opciones(db, claves - ocupadas, modulos)


def opciones_cubre_por_dia(db: Session, clave_norm: str, inicio: date, fin: date) -> Dict[date, List[dict]]:
    """Misma regla que opciones_cubre, revisada día por día (quien está libre ESA fecha)."""
    dias = [inicio + timedelta(days=i) for i in range((fin - inicio).days + 1)]
    claves, modulos = _candidatas_cubre(db, clave_norm)
    if not claves:
        return {d: [] for d in dias}
    ocupaciones = _ocupaciones(db, claves, inicio, fin)
    todas = _armar_opciones(db, claves, modulos)
    salida = {}
    for d in dias:
        ocupadas = {c for c, i, f in ocupaciones if i <= d <= f}
        salida[d] = [o for o in todas if _normalizar(o["clave"]) not in ocupadas]
    return salida


@router.get("/cubre-opciones", response_model=List[CubreOpcion])
def cubre_opciones(
    inicio: date,
    fin: date,
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(get_current_user),
):
    _exigir_piloto(usuario, db)
    if fin < inicio:
        raise HTTPException(status_code=400, detail="La fecha final no puede ser anterior a la fecha de inicio")
    return opciones_cubre(db, _normalizar(usuario.nombre_englobado), inicio, fin)


MAX_DIAS_CUBRE_POR_DIA = 62


class CubreOpcionesDia(BaseModel):
    fecha: date
    opciones: List[CubreOpcion]


@router.get("/cubre-opciones-por-dia", response_model=List[CubreOpcionesDia])
def cubre_opciones_por_dia(
    inicio: date,
    fin: date,
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(get_current_user),
):
    # Para "Cubrir por día": la lista de cada fecha (misma regla que /cubre-opciones)
    _exigir_piloto(usuario, db)
    if fin < inicio:
        raise HTTPException(status_code=400, detail="La fecha final no puede ser anterior a la fecha de inicio")
    if (fin - inicio).days + 1 > MAX_DIAS_CUBRE_POR_DIA:
        raise HTTPException(status_code=400, detail=f"Máximo {MAX_DIAS_CUBRE_POR_DIA} días")
    por_dia = opciones_cubre_por_dia(db, _normalizar(usuario.nombre_englobado), inicio, fin)
    return [{"fecha": d, "opciones": por_dia[d]} for d in sorted(por_dia)]


@router.post("/vacaciones", response_model=VacacionResponse, status_code=201)
def pedir_vacaciones(
    body: SolicitudVacacion,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(get_current_user),
):
    _exigir_piloto(usuario, db)

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
        # 5 días de anticipación, cualquier día de la semana, sin 15 dic - 6 ene
        validar_fechas_dia_extra_empleado(inicio, fin, hoy)
        if not (body.cubre_clave or "").strip() and not body.sin_cubre and body.cubre_por_dia is None:
            raise HTTPException(
                status_code=400,
                detail="Elige quién te cubre (o \"No tengo quién me cubra\")",
            )
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
    # Solo si quedan personas distintas por día: [(fecha, clave o None)]
    asignaciones = None
    if body.cubre_por_dia is not None:
        if (body.cubre_clave or "").strip() or body.sin_cubre:
            raise HTTPException(
                status_code=400,
                detail="Elige quién te cubre para todo el periodo o por día, no las dos",
            )
        esperadas = [inicio + timedelta(days=i) for i in range(dias)]
        if sorted(d.fecha for d in body.cubre_por_dia) != esperadas:
            raise HTTPException(status_code=400, detail="Indica quién te cubre cada día del periodo, sin repetir fechas")
        # Solo alguien de la lista de GET /mi-perfil/cubre-opciones-por-dia para ESA fecha
        por_dia = opciones_cubre_por_dia(db, clave_norm, inicio, fin)
        elegidos = {}
        for d in body.cubre_por_dia:
            if not _normalizar(d.cubre_clave):
                elegidos[d.fecha] = None
                continue
            permitidas = {_normalizar(o["clave"]) for o in por_dia.get(d.fecha, [])}
            persona = _usuario_activo(db, d.cubre_clave) if _normalizar(d.cubre_clave) in permitidas else None
            if persona is None:
                raise HTTPException(status_code=400, detail=f"Esa persona no puede cubrirte el {_texto_fecha(d.fecha)}")
            elegidos[d.fecha] = persona
        distintas = {_normalizar(p.nombre_englobado) if p else "" for p in elegidos.values()}
        if len(distintas) == 1:
            # Todos los días con la misma persona (o todos sin nadie): se guarda como siempre
            cubre = next(iter(elegidos.values()))
        else:
            if not cubre_por_dia.tabla_existe(db):
                raise HTTPException(
                    status_code=503,
                    detail="Por ahora no se puede elegir quién cubre por día: elige una sola persona para todo el periodo",
                )
            asignaciones = [
                (fecha, p.nombre_englobado.strip() if p else None) for fecha, p in sorted(elegidos.items())
            ]
    elif (body.cubre_clave or "").strip():
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
    if asignaciones:
        db.flush()
        cubre_por_dia.crear(db, vacacion.id, asignaciones)
    db.commit()
    db.refresh(vacacion)
    # Aviso a administración, después del commit y fuera de la petición: nunca rompe la solicitud
    background_tasks.add_task(avisos.solicitud_nueva, clave, body.tipo, inicio, fin, dias, vacacion.id)
    if vacacion.cubre_clave:
        # Eligió quién la cubre: se le pide confirmar (CONFIRMO / NO PUEDO en su aviso)
        background_tasks.add_task(avisos.cubrir_solicitud, vacacion.cubre_clave, clave, inicio, fin, vacacion.id)
    if asignaciones:
        # Por día: a cada persona un solo aviso con SUS días
        personas = {}
        for fecha, quien in asignaciones:
            if quien:
                personas.setdefault(quien, []).append(fecha)
        for quien, fechas in personas.items():
            background_tasks.add_task(avisos.cubrir_dias, quien, clave, fechas, vacacion.id)
    return serializar_con_dias(db, [vacacion])[0]


@router.delete("/vacaciones/{vacacion_id}")
def cancelar_solicitud(
    vacacion_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(get_current_user),
):
    _exigir_piloto(usuario, db)

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
    # Por día: cada persona con sus días (los que no rechazó)
    por_persona = cubre_por_dia.por_persona(
        f for f in cubre_por_dia.dias_de_una(db, vacacion.id) if f.confirmado is not False
    )
    cubre_por_dia.borrar(db, vacacion.id)
    db.delete(vacacion)
    db.commit()
    background_tasks.add_task(avisos.solicitud_cancelada, *datos_aviso)
    if cubre:
        background_tasks.add_task(
            avisos.cobertura_cancelada, cubre, datos_aviso[0], datos_aviso[2], datos_aviso[3], datos_aviso[5]
        )
    for quien, fechas in por_persona.items():
        background_tasks.add_task(avisos.cobertura_cancelada_dias, quien, datos_aviso[0], fechas, datos_aviso[5])
    return {"ok": True}
