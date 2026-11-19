"""MENSAJES: avisos automáticos y conversación empleado <-> administración.

Tablas hechas a mano en Supabase (avisos, mensajes); sus modelos viven en BaseManual,
fuera del create_all. Solo entran admin y quien tenga acceso (hoy, el piloto de
Mi Perfil; ver app/avisos.py). La clave del empleado SIEMPRE sale del token.
La bandeja de administración es compartida: lo que lee un admin queda leído para todos.
"""
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import false, func, or_, select, true
from sqlalchemy.orm import Session

from app import avisos as avisos_auto
from app import models
from app.avisos import clave_de, claves_con_mensajes, es_admin, normalizar, tiene_mensajes
from app.routers.vacaciones import autorizar_si_completa, es_pendiente_con_aprobacion, registrar_respuesta_cubre
from app.config import get_current_user
from app.database import get_db

router = APIRouter()

LIMITE_DEFECTO = 30
LIMITE_MAXIMO = 100
TEXTO_MAXIMO = 2000


def usuario_mensajes(usuario: models.Usuario = Depends(get_current_user)) -> models.Usuario:
    if not tiene_mensajes(usuario):
        raise HTTPException(status_code=403, detail="MENSAJES todavía no está disponible para tu usuario")
    return usuario


def solo_admin_mensajes(usuario: models.Usuario = Depends(usuario_mensajes)) -> models.Usuario:
    if not es_admin(usuario):
        raise HTTPException(status_code=403, detail="Solo administración")
    return usuario


def solo_empleado_mensajes(usuario: models.Usuario = Depends(usuario_mensajes)) -> models.Usuario:
    if es_admin(usuario):
        raise HTTPException(status_code=403, detail="Administración usa /mensajes/conversaciones")
    if not clave_de(usuario):
        raise HTTPException(status_code=400, detail="Tu usuario no tiene clave")
    return usuario


# ------------------------------- schemas -------------------------------

class NoLeidosResponse(BaseModel):
    avisos: int
    mensajes: int
    total: int


class CoberturaAviso(BaseModel):
    # Botones CONFIRMO / NO PUEDO del aviso "¿Confirmas que vas a cubrir...?"
    vacacion_id: int
    puede_responder: bool
    respuesta: Optional[bool] = None


class AvisoResponse(BaseModel):
    id: int
    tipo: str
    texto: str
    enlace: Optional[str] = None
    sobre_clave: Optional[str] = None
    creado_at: Optional[datetime] = None
    leido_at: Optional[datetime] = None
    cobertura: Optional[CoberturaAviso] = None


class MensajeResponse(BaseModel):
    id: int
    clave: str
    de_admin: bool
    autor: str
    texto: str
    creado_at: Optional[datetime] = None
    leido_at: Optional[datetime] = None


class ConversacionResponse(BaseModel):
    clave: str
    nombre: Optional[str] = None
    tiene_acceso: bool
    ultimo_texto: Optional[str] = None
    ultimo_at: Optional[datetime] = None
    ultimo_de_admin: Optional[bool] = None
    no_leidos: int


class MensajeCrear(BaseModel):
    model_config = ConfigDict(extra="forbid")
    texto: str = Field(..., max_length=TEXTO_MAXIMO)


class EnviadoATodosResponse(BaseModel):
    enviados: int


def _texto_limpio(body: MensajeCrear) -> str:
    texto = body.texto.strip()
    if not texto:
        raise HTTPException(status_code=400, detail="El mensaje está vacío")
    return texto


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------- contador -------------------------------

@router.get("/no-leidos", response_model=NoLeidosResponse)
def no_leidos(
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(usuario_mensajes),
):
    # Una sola consulta con dos COUNT sobre los índices parciales WHERE leido_at IS NULL
    A, M = models.Aviso, models.Mensaje
    if es_admin(usuario):
        filtro_avisos = A.para_clave.is_(None)
        filtro_mensajes = M.de_admin == false()
    else:
        clave = clave_de(usuario)
        filtro_avisos = A.para_clave == clave
        filtro_mensajes = (M.clave == clave) & (M.de_admin == true())
    fila = db.execute(
        select(
            select(func.count()).select_from(A).where(filtro_avisos, A.leido_at.is_(None)).scalar_subquery(),
            select(func.count()).select_from(M).where(filtro_mensajes, M.leido_at.is_(None)).scalar_subquery(),
        )
    ).one()
    avisos, mensajes = int(fila[0] or 0), int(fila[1] or 0)
    return {"avisos": avisos, "mensajes": mensajes, "total": avisos + mensajes}


# ------------------------------- avisos -------------------------------

def _filtro_avisos(usuario: models.Usuario):
    if es_admin(usuario):
        return models.Aviso.para_clave.is_(None)
    return models.Aviso.para_clave == clave_de(usuario)


@router.get("/avisos", response_model=List[AvisoResponse])
def listar_avisos(
    antes_de: Optional[int] = Query(None, ge=1),
    limite: int = Query(LIMITE_DEFECTO, ge=1, le=LIMITE_MAXIMO),
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(usuario_mensajes),
):
    q = db.query(models.Aviso).filter(_filtro_avisos(usuario))
    if antes_de is not None:
        q = q.filter(models.Aviso.id < antes_de)
    filas = q.order_by(models.Aviso.id.desc()).limit(limite).all()
    # Avisos de cobertura: si todavía puede responder (1 consulta para toda la página)
    ids = {a.vacacion_id for a in filas if a.tipo == "cubrir_solicitud" and a.vacacion_id}
    vacs = {v.id: v for v in db.query(models.Vacacion).filter(models.Vacacion.id.in_(ids)).all()} if ids else {}
    salida = []
    for a in filas:
        datos = AvisoResponse.model_validate(a, from_attributes=True).model_dump()
        if a.tipo == "cubrir_solicitud" and a.vacacion_id:
            v = vacs.get(a.vacacion_id)
            sigue = v is not None and normalizar(v.cubre_clave) == normalizar(a.para_clave)
            datos["cobertura"] = {
                "vacacion_id": a.vacacion_id,
                "puede_responder": bool(sigue and es_pendiente_con_aprobacion(v) and v.cubre_confirmado is None),
                "respuesta": v.cubre_confirmado if sigue else None,
            }
        salida.append(datos)
    return salida


@router.post("/avisos/leer-todos")
def leer_todos_los_avisos(
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(usuario_mensajes),
):
    n = (
        db.query(models.Aviso)
        .filter(_filtro_avisos(usuario), models.Aviso.leido_at.is_(None))
        .update({"leido_at": _ahora(), "leido_por": usuario.id}, synchronize_session=False)
    )
    db.commit()
    return {"ok": True, "marcados": n}


@router.post("/avisos/{aviso_id}/leido", response_model=AvisoResponse)
def marcar_aviso_leido(
    aviso_id: int,
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(usuario_mensajes),
):
    # De otra persona o inexistente: misma respuesta, para no revelar avisos ajenos
    aviso = db.query(models.Aviso).filter(models.Aviso.id == aviso_id, _filtro_avisos(usuario)).first()
    if aviso is None:
        raise HTTPException(status_code=404, detail="Aviso no encontrado")
    if aviso.leido_at is None:
        aviso.leido_at = _ahora()
        aviso.leido_por = usuario.id
        db.commit()
        db.refresh(aviso)
    return aviso


# ------------------------------- mensajes -------------------------------

def _autores(db: Session, filas: List[models.Mensaje]) -> dict:
    ids = {m.autor_id for m in filas}
    if not ids:
        return {}
    usuarios = (
        db.query(models.Usuario.id, models.Usuario.nombre_englobado, models.Usuario.username)
        .filter(models.Usuario.id.in_(ids))
        .all()
    )
    return {u.id: (u.nombre_englobado or "").strip() or u.username for u in usuarios}


def _serializar(m: models.Mensaje, autores: dict, para_empleado: bool) -> dict:
    autor = "Administración" if (para_empleado and m.de_admin) else autores.get(m.autor_id, "")
    return {
        "id": m.id,
        "clave": m.clave,
        "de_admin": m.de_admin,
        "autor": autor,
        "texto": m.texto,
        "creado_at": m.creado_at,
        "leido_at": m.leido_at,
    }


def _conversacion(db: Session, clave: str, antes_de: Optional[int], limite: int, lee_admin: bool) -> List[models.Mensaje]:
    # Al abrir se marcan leídos los mensajes del OTRO lado
    marcadas = (
        db.query(models.Mensaje)
        .filter(
            models.Mensaje.clave == clave,
            models.Mensaje.de_admin == (false() if lee_admin else true()),
            models.Mensaje.leido_at.is_(None),
        )
        .update({"leido_at": _ahora()}, synchronize_session=False)
    )
    if marcadas:
        db.commit()
    q = db.query(models.Mensaje).filter(models.Mensaje.clave == clave)
    if antes_de is not None:
        q = q.filter(models.Mensaje.id < antes_de)
    # Los más recientes, devueltos del más viejo al más nuevo (orden de chat)
    return list(reversed(q.order_by(models.Mensaje.id.desc()).limit(limite).all()))


def _nombres(db: Session, claves: set) -> dict:
    if not claves:
        return {}
    usuarios = (
        db.query(models.Usuario)
        .filter(
            or_(
                func.upper(func.trim(models.Usuario.nombre_englobado)).in_(claves),
                func.upper(func.trim(models.Usuario.username)).in_(claves),
            )
        )
        .order_by(models.Usuario.activo.desc(), models.Usuario.id)
        .all()
    )
    nombres: dict = {}
    for u in usuarios:
        c = clave_de(u)
        if c in claves and c not in nombres and (u.nombre_completo or "").strip():
            nombres[c] = u.nombre_completo.strip()
    return nombres


@router.get("/conversaciones", response_model=List[ConversacionResponse])
def listar_conversaciones(
    db: Session = Depends(get_db),
    _admin: models.Usuario = Depends(solo_admin_mensajes),
):
    M = models.Mensaje
    resumen = (
        db.query(
            M.clave,
            func.max(M.id),
            func.count(M.id).filter(M.de_admin == false(), M.leido_at.is_(None)),
        )
        .group_by(M.clave)
        .all()
    )
    ultimos_ids = [r[1] for r in resumen]
    ultimos = {m.id: m for m in db.query(M).filter(M.id.in_(ultimos_ids)).all()} if ultimos_ids else {}
    con_acceso = claves_con_mensajes(db)
    claves = con_acceso | {r[0] for r in resumen}
    nombres = _nombres(db, claves)
    por_clave = {r[0]: (ultimos.get(r[1]), int(r[2] or 0)) for r in resumen}
    filas = []
    for c in claves:
        ultimo, sin_leer = por_clave.get(c, (None, 0))
        filas.append({
            "clave": c,
            "nombre": nombres.get(c),
            "tiene_acceso": c in con_acceso,
            "ultimo_texto": ultimo.texto if ultimo else None,
            "ultimo_at": ultimo.creado_at if ultimo else None,
            "ultimo_de_admin": ultimo.de_admin if ultimo else None,
            "no_leidos": sin_leer,
        })
    # Primero lo que tiene pendiente, luego lo más reciente, luego por clave
    filas.sort(key=lambda f: (f["no_leidos"] == 0, -(f["ultimo_at"].timestamp() if f["ultimo_at"] else 0), f["clave"]))
    return filas


@router.get("/conversaciones/{clave}", response_model=List[MensajeResponse])
def ver_conversacion(
    clave: str,
    antes_de: Optional[int] = Query(None, ge=1),
    limite: int = Query(LIMITE_DEFECTO, ge=1, le=LIMITE_MAXIMO),
    db: Session = Depends(get_db),
    _admin: models.Usuario = Depends(solo_admin_mensajes),
):
    filas = _conversacion(db, normalizar(clave), antes_de, limite, lee_admin=True)
    autores = _autores(db, filas)
    return [_serializar(m, autores, para_empleado=False) for m in filas]


@router.post("/conversaciones/{clave}", response_model=MensajeResponse, status_code=201)
def escribir_a_empleado(
    clave: str,
    body: MensajeCrear,
    db: Session = Depends(get_db),
    admin: models.Usuario = Depends(solo_admin_mensajes),
):
    texto = _texto_limpio(body)
    clave = normalizar(clave)
    if clave not in claves_con_mensajes(db):
        raise HTTPException(status_code=400, detail="Esta persona todavía no tiene MENSAJES")
    m = models.Mensaje(clave=clave, de_admin=True, autor_id=admin.id, texto=texto, creado_at=_ahora())
    db.add(m)
    db.commit()
    db.refresh(m)
    return _serializar(m, _autores(db, [m]), para_empleado=False)


@router.post("/todos", response_model=EnviadoATodosResponse, status_code=201)
def escribir_a_todos(
    body: MensajeCrear,
    db: Session = Depends(get_db),
    admin: models.Usuario = Depends(solo_admin_mensajes),
):
    texto = _texto_limpio(body)
    claves = sorted(claves_con_mensajes(db))
    if not claves:
        raise HTTPException(status_code=400, detail="No hay empleados con MENSAJES")
    lote, ahora = str(uuid.uuid4()), _ahora()
    db.add_all(
        models.Mensaje(clave=c, de_admin=True, autor_id=admin.id, texto=texto, lote=lote, creado_at=ahora)
        for c in claves
    )
    db.commit()
    return {"enviados": len(claves)}


@router.get("/mi-conversacion", response_model=List[MensajeResponse])
def mi_conversacion(
    antes_de: Optional[int] = Query(None, ge=1),
    limite: int = Query(LIMITE_DEFECTO, ge=1, le=LIMITE_MAXIMO),
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(solo_empleado_mensajes),
):
    filas = _conversacion(db, clave_de(usuario), antes_de, limite, lee_admin=False)
    autores = _autores(db, filas)
    return [_serializar(m, autores, para_empleado=True) for m in filas]


@router.post("/mi-conversacion", response_model=MensajeResponse, status_code=201)
def escribir_a_administracion(
    body: MensajeCrear,
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(solo_empleado_mensajes),
):
    texto = _texto_limpio(body)
    m = models.Mensaje(clave=clave_de(usuario), de_admin=False, autor_id=usuario.id, texto=texto, creado_at=_ahora())
    db.add(m)
    db.commit()
    db.refresh(m)
    return _serializar(m, _autores(db, [m]), para_empleado=True)


# ------------------------------- cobertura -------------------------------

class RespuestaCobertura(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmo: bool


class CoberturaResponse(BaseModel):
    vacacion_id: int
    cubre_confirmado: Optional[bool] = None
    estado: Optional[str] = None


@router.post("/cobertura/{vacacion_id}", response_model=CoberturaResponse)
def responder_cobertura(
    vacacion_id: int,
    body: RespuestaCobertura,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(usuario_mensajes),
):
    # Solo responde quien cubre (su clave sale del token), una vez por asignación
    vacacion = (
        db.query(models.Vacacion).filter(models.Vacacion.id == vacacion_id).with_for_update().first()
    )
    if vacacion is None or es_admin(usuario) or normalizar(vacacion.cubre_clave) != clave_de(usuario):
        raise HTTPException(status_code=403, detail="No te toca responder esta cobertura")
    if not es_pendiente_con_aprobacion(vacacion):
        raise HTTPException(status_code=409, detail="La solicitud ya no está pendiente")
    if vacacion.cubre_confirmado is not None:
        raise HTTPException(status_code=409, detail="Ya respondiste esta cobertura")
    registrar_respuesta_cubre(vacacion, body.confirmo, usuario.id)
    autorizada = autorizar_si_completa(vacacion)
    # El aviso de la pregunta queda leído
    (
        db.query(models.Aviso)
        .filter(
            models.Aviso.tipo == "cubrir_solicitud",
            models.Aviso.vacacion_id == vacacion.id,
            models.Aviso.para_clave == clave_de(usuario),
            models.Aviso.leido_at.is_(None),
        )
        .update({"leido_at": _ahora(), "leido_por": usuario.id}, synchronize_session=False)
    )
    db.commit()
    db.refresh(vacacion)
    datos = (vacacion.clave, vacacion.fecha_inicio, vacacion.fecha_fin, vacacion.id)
    if not body.confirmo:
        background_tasks.add_task(avisos_auto.cubre_no_puede, clave_de(usuario), *datos)
    if autorizada:
        background_tasks.add_task(
            avisos_auto.solicitud_aprobada, vacacion.clave, vacacion.tipo, vacacion.fecha_inicio, vacacion.fecha_fin, vacacion.id
        )
    return {"vacacion_id": vacacion.id, "cubre_confirmado": vacacion.cubre_confirmado, "estado": vacacion.estado}
