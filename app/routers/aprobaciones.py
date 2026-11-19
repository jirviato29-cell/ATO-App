"""Pantalla de RH y encargados: solo SU columna (RH o Encargado) de las solicitudes que les tocan.

Quién puede qué vive en app/permisos_vacaciones.py. Admin (Gerencia) NO usa este router:
sigue con /vacaciones/solicitudes y PATCH /vacaciones/{id}/aprobacion, sin cambios.
La regla de marcar (409, autorizar con los 4 en verde, avisos) es la MISMA función que usa admin.
"""
from datetime import date, datetime
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app import models
from app.config import get_current_user
from app.database import get_db
from app.permisos_vacaciones import Alcance, alcance, modulos_principales, niveles_que_marca, normalizar, pendientes_de
from app.routers.vacaciones import _clave_columna, _nombres_aprobadores, _obtener_vacacion, aplicar_aprobacion

router = APIRouter()


class NivelRevision(BaseModel):
    nivel: Literal["rh", "encargado"]
    aprobado: Optional[bool] = None
    por: Optional[str] = None
    at: Optional[datetime] = None


class SolicitudRevision(BaseModel):
    # Solo lo necesario para decidir: sin saldo, cubre, notas ni las otras columnas
    id: int
    clave: str
    nombre_completo: Optional[str] = None
    modulo: Optional[str] = None
    tipo: Optional[str] = None
    fecha_inicio: date
    fecha_fin: date
    dias: Optional[float] = None
    niveles: List[NivelRevision]


class RevisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # "gerencia" se acepta en el esquema para responder 403 (y no 422) si alguien lo intenta
    nivel: Literal["rh", "encargado", "gerencia"]
    aprobado: bool


def revisor(
    usuario: models.Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Alcance:
    a = alcance(db, usuario)
    if not a.es_revisor:
        raise HTTPException(status_code=403, detail="No revisas solicitudes")
    return a


def _nombres(db: Session, claves: set) -> Dict[str, str]:
    # Mismo criterio que /vacaciones/solicitudes: las cuentas activas ganan a las de baja
    if not claves:
        return {}
    col = _clave_columna(models.Usuario.nombre_englobado)
    filas = (
        db.query(col.label("clave"), models.Usuario.nombre_completo)
        .filter(col.in_(claves))
        .order_by(models.Usuario.activo.is_(True), models.Usuario.id.desc())
        .all()
    )
    return {f.clave: f.nombre_completo for f in filas}


def _modulos(db: Session, ids: set) -> Dict[int, str]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return {f.id: f.nombre for f in db.query(models.Modulo.id, models.Modulo.nombre).filter(models.Modulo.id.in_(ids)).all()}


def _armar(db: Session, filas: List[dict]) -> List[dict]:
    vacaciones = [f["vacacion"] for f in filas]
    aprobadores = _nombres_aprobadores(db, vacaciones)
    nombres = _nombres(db, {normalizar(v.clave) for v in vacaciones})
    modulos = _modulos(db, {f["modulo_id"] for f in filas})
    salida = []
    for f in filas:
        v = f["vacacion"]
        niveles = []
        for n in f["niveles"]:
            por = getattr(v, f"aprobado_{n}_por")
            niveles.append({
                "nivel": n,
                "aprobado": getattr(v, f"aprobado_{n}"),
                "por": aprobadores.get(por) if por is not None else None,
                "at": getattr(v, f"aprobado_{n}_at"),
            })
        salida.append({
            "id": v.id,
            "clave": v.clave,
            "nombre_completo": nombres.get(normalizar(v.clave)),
            "modulo": modulos.get(f["modulo_id"]),
            "tipo": v.tipo,
            "fecha_inicio": v.fecha_inicio,
            "fecha_fin": v.fecha_fin,
            "dias": v.dias,
            "niveles": niveles,
        })
    return salida


@router.get("/solicitudes", response_model=List[SolicitudRevision])
def solicitudes_por_revisar(
    db: Session = Depends(get_db),
    a: Alcance = Depends(revisor),
):
    return _armar(db, pendientes_de(db, a))


@router.patch("/{vacacion_id}", response_model=SolicitudRevision)
def marcar(
    vacacion_id: int,
    body: RevisionBody,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    usuario: models.Usuario = Depends(get_current_user),
    a: Alcance = Depends(revisor),
):
    vacacion = _obtener_vacacion(db, vacacion_id, bloquear=True)
    clave = normalizar(vacacion.clave)
    modulo = modulos_principales(db, [clave]).get(clave)
    niveles = niveles_que_marca(a, clave, modulo)
    # El permiso va ANTES que el estado: de otro módulo o de otro nivel es 403 siempre
    if body.nivel not in niveles:
        raise HTTPException(status_code=403, detail="No puedes marcar esa columna en esta solicitud")
    aplicar_aprobacion(db, vacacion, body.nivel, body.aprobado, usuario, background_tasks)
    return _armar(db, [{"vacacion": vacacion, "niveles": niveles, "modulo_id": modulo}])[0]
