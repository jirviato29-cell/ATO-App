"""Quién cubre CADA día de una solicitud (tabla vacaciones_cubre, hecha a mano en Supabase).

Solo hay filas cuando la persona eligió gente distinta para distintos días. Si todos los
días quedan con la misma persona (o todos con "No tengo quién me cubra"), la solicitud se
guarda como siempre en vacaciones.cubre_clave y aquí no hay nada: las solicitudes de
siempre siguen el camino de siempre.

Si la tabla no existe, todo se comporta como "sin filas" y nada truena.
"""
import logging
import time
from datetime import date, datetime, timezone
from typing import Dict, Iterable, List, Optional

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app import models

log = logging.getLogger("cubre_por_dia")

TABLA = models.VacacionCubre.__tablename__
# Si no existe, se vuelve a revisar cada minuto (al crearla no hace falta reiniciar)
_REVISAR_CADA_SEG = 60.0
_cache = {"existe": None, "revisado": 0.0}


def normalizar(clave: Optional[str]) -> str:
    return (clave or "").strip().upper()


def tabla_existe(db: Session) -> bool:
    if _cache["existe"] is True:
        return True
    ahora = time.monotonic()
    if _cache["existe"] is False and ahora - _cache["revisado"] < _REVISAR_CADA_SEG:
        return False
    try:
        existe = inspect(db.get_bind()).has_table(TABLA)
    except Exception:
        log.exception("No se pudo revisar si existe %s; se trata como sin filas", TABLA)
        existe = False
    _cache.update(existe=existe, revisado=ahora)
    return existe


def dias_de(db: Session, vacacion_ids: Iterable[int]) -> Dict[int, List[models.VacacionCubre]]:
    """{vacacion_id: filas ordenadas por fecha}; solo trae las solicitudes que tienen filas."""
    ids = {i for i in vacacion_ids if i is not None}
    if not ids or not tabla_existe(db):
        return {}
    filas = (
        db.query(models.VacacionCubre)
        .filter(models.VacacionCubre.vacacion_id.in_(ids))
        .order_by(models.VacacionCubre.vacacion_id, models.VacacionCubre.fecha)
        .all()
    )
    salida: Dict[int, List[models.VacacionCubre]] = {}
    for f in filas:
        salida.setdefault(f.vacacion_id, []).append(f)
    return salida


def dias_de_una(db: Session, vacacion_id: int) -> List[models.VacacionCubre]:
    return dias_de(db, [vacacion_id]).get(vacacion_id, [])


def crear(db: Session, vacacion_id: int, asignaciones: Iterable) -> None:
    """asignaciones: [(fecha, clave_o_None)]. La tabla debe existir (revisar antes)."""
    for fecha, clave in asignaciones:
        db.add(models.VacacionCubre(vacacion_id=vacacion_id, fecha=fecha, cubre_clave=clave))


def borrar(db: Session, vacacion_id: int) -> None:
    # En Supabase la FK ya borra en cascada; esto es por si acaso (y para SQLite en pruebas)
    if tabla_existe(db):
        db.query(models.VacacionCubre).filter(models.VacacionCubre.vacacion_id == vacacion_id).delete(
            synchronize_session=False
        )


def completo(filas: List[models.VacacionCubre]) -> bool:
    """Verde: todos los días tienen persona y todas confirmaron."""
    return bool(filas) and all(normalizar(f.cubre_clave) and f.confirmado is True for f in filas)


def agregado(filas: List[models.VacacionCubre]) -> Optional[bool]:
    """Rojo (False) si alguien no puede; verde (True) si está completo; si no, None (esperando)."""
    if any(f.confirmado is False for f in filas):
        return False
    if completo(filas):
        return True
    return None


def de_persona(filas: List[models.VacacionCubre], clave: str) -> List[models.VacacionCubre]:
    clave = normalizar(clave)
    return [f for f in filas if clave and normalizar(f.cubre_clave) == clave]


def respuesta_de(filas_persona: List[models.VacacionCubre]) -> Optional[bool]:
    """Lo que respondió UNA persona en sus días: False si dijo que no, True si confirmó todo."""
    if any(f.confirmado is False for f in filas_persona):
        return False
    if filas_persona and all(f.confirmado is True for f in filas_persona):
        return True
    return None


def por_persona(filas: Iterable[models.VacacionCubre]) -> Dict[str, List[date]]:
    """{clave: [fechas]} de los días que tienen persona."""
    salida: Dict[str, List[date]] = {}
    for f in filas:
        clave = normalizar(f.cubre_clave)
        if clave:
            salida.setdefault(clave, []).append(f.fecha)
    return salida


def responder(filas: List[models.VacacionCubre], confirmo: bool, usuario_id: int) -> None:
    ahora = datetime.now(timezone.utc)
    for f in filas:
        f.confirmado = confirmo
        f.confirmado_por = usuario_id
        f.confirmado_at = ahora


def sincronizar(v: models.Vacacion, filas: List[models.VacacionCubre]) -> None:
    """Copia el resultado de todos los días en vacaciones.cubre_confirmado (para lo que solo lee esa columna)."""
    v.cubre_confirmado = agregado(filas)
    v.cubre_confirmado_por = None
    v.cubre_confirmado_at = None
