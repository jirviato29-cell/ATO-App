"""Avisos automáticos de MENSAJES y quién tiene acceso al módulo.

Regla de oro: un aviso NUNCA rompe lo principal. Las funciones publicas de este
archivo se llaman con BackgroundTasks DESPUES del commit de la solicitud (la
respuesta ya salio), abren su PROPIA sesion y se tragan cualquier error (solo
queda en el log). Reciben valores planos, nunca objetos del ORM.
"""
import logging
from datetime import date, timedelta
from typing import List, Optional, Set

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.database import SessionLocal

log = logging.getLogger("avisos")

# Por ahora MENSAJES es solo para admin y para el piloto de Mi Perfil. Para abrirlo
# a todos los empleados (asesor/encargado activos), cambiar a True.
MENSAJES_PARA_TODOS = False
ROLES_EMPLEADO = (models.RolEnum.asesor, models.RolEnum.encargado)

ENLACE_SOLICITUDES = "/mi-perfil?tab=solicitudes"


def normalizar(clave: Optional[str]) -> str:
    return (clave or "").strip().upper()


def clave_de(usuario: models.Usuario) -> str:
    # Igual que /mi-perfil: englobado o, si no tiene, username
    return normalizar(usuario.nombre_englobado) or normalizar(usuario.username)


def _usernames_piloto() -> Set[str]:
    # Import tardío: mi_perfil importa vacaciones y vacaciones importa este archivo
    from app.routers.mi_perfil import PILOTO_MI_PERFIL

    return {normalizar(u) for u in PILOTO_MI_PERFIL}


def es_admin(usuario: models.Usuario) -> bool:
    return usuario.rol == models.RolEnum.admin


def tiene_mensajes(usuario: models.Usuario) -> bool:
    if es_admin(usuario):
        return True
    if MENSAJES_PARA_TODOS:
        return usuario.rol in ROLES_EMPLEADO
    return normalizar(usuario.username) in _usernames_piloto()


def claves_con_mensajes(db: Session) -> Set[str]:
    # Claves de empleado que hoy pueden ver MENSAJES (destinatarios de "a todos" y de avisos)
    q = db.query(models.Usuario).filter(models.Usuario.activo == True)  # noqa: E712
    if MENSAJES_PARA_TODOS:
        q = q.filter(models.Usuario.rol.in_(ROLES_EMPLEADO))
    else:
        pilotos = _usernames_piloto()
        if not pilotos:
            return set()
        q = q.filter(func.upper(func.trim(models.Usuario.username)).in_(pilotos))
    return {c for c in (clave_de(u) for u in q.all() if not es_admin(u)) if c}


# ------------------------------- textos -------------------------------

def _dm(d: date) -> str:
    return f"{d.day:02d}/{d.month:02d}"


def _dma(d: date) -> str:
    return f"{d.day:02d}/{d.month:02d}/{d.year}"


def _dias(n) -> str:
    n = int(n) if float(n).is_integer() else n
    return f"{n} día" if n == 1 else f"{n} días"


def _que(tipo: str) -> str:
    return "día extra" if tipo == "dia_extra" else "vacaciones"


def _fechas(inicio: date, fin: date, dias) -> str:
    rango = f"el {_dm(inicio)}" if inicio == fin else f"del {_dm(inicio)} al {_dm(fin)}"
    return f"{rango} ({_dias(dias)})"


# ------------------------------- guardar -------------------------------

def _guardar(filas: List[dict], solo_si_tiene_acceso: Optional[str] = None) -> None:
    db = SessionLocal()
    try:
        if solo_si_tiene_acceso is not None and solo_si_tiene_acceso not in claves_con_mensajes(db):
            return
        db.add_all(models.Aviso(**f) for f in filas)
        db.commit()
    except Exception:
        log.exception("No se pudo crear el aviso (la operación principal ya quedó guardada)")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        try:
            db.close()
        except Exception:
            pass


def _seguro(fn):
    # Ni armar el texto puede tumbar nada
    def envuelta(*args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception:
            log.exception("No se pudo crear el aviso (la operación principal ya quedó guardada)")

    envuelta.__name__ = fn.__name__
    return envuelta


# --------------------------- avisos públicos ---------------------------

@_seguro
def solicitud_nueva(clave: str, tipo: str, inicio: date, fin: date, dias, vacacion_id: int) -> None:
    clave = normalizar(clave)
    _guardar([{
        "para_clave": None,
        "tipo": "solicitud_nueva",
        "texto": f"{clave} solicitó {_que(tipo)} {_fechas(inicio, fin, dias)}",
        "enlace": ENLACE_SOLICITUDES,
        "sobre_clave": clave,
        "vacacion_id": vacacion_id,
    }])


@_seguro
def solicitud_cancelada(clave: str, tipo: str, inicio: date, fin: date, dias, vacacion_id: int) -> None:
    clave = normalizar(clave)
    _guardar([{
        "para_clave": None,
        "tipo": "solicitud_cancelada",
        "texto": f"{clave} canceló su solicitud de {_que(tipo)} {_fechas(inicio, fin, dias)}",
        "enlace": ENLACE_SOLICITUDES,
        "sobre_clave": clave,
        "vacacion_id": vacacion_id,
    }])


@_seguro
def decision_gerencia(clave: str, tipo: str, inicio: date, fin: date, dias, autorizada: bool, vacacion_id: int) -> None:
    clave = normalizar(clave)
    if autorizada:
        texto = f"Gerencia autorizó tu solicitud de {_que(tipo)} {_fechas(inicio, fin, dias)}"
    else:
        texto = f"Gerencia rechazó tu solicitud de {_que(tipo)} {_fechas(inicio, fin, dias)}"
    _guardar(
        [{
            "para_clave": clave,
            "tipo": "solicitud_autorizada" if autorizada else "solicitud_rechazada",
            "texto": texto,
            "enlace": ENLACE_SOLICITUDES,
            "sobre_clave": clave,
            "vacacion_id": vacacion_id,
        }],
        solo_si_tiene_acceso=clave,
    )


@_seguro
def dia_extra_otorgado(clave: str, fecha_festivo: date, dias: int) -> None:
    clave = normalizar(clave)
    # Igual que la columna generada dias_extra.vence: festivo + 1 año - 1 día (29/02 -> 28/02)
    try:
        aniversario = fecha_festivo.replace(year=fecha_festivo.year + 1)
    except ValueError:
        aniversario = date(fecha_festivo.year + 1, 2, 28)
    vence = aniversario - timedelta(days=1)
    verbo = "Se te otorgó" if dias == 1 else "Se te otorgaron"
    _guardar(
        [{
            "para_clave": clave,
            "tipo": "dia_extra_otorgado",
            "texto": (
                f"{verbo} {_dias(dias)} de descanso extra por el festivo del {_dma(fecha_festivo)}; "
                f"vence el {_dma(vence)}"
            ),
            "enlace": ENLACE_SOLICITUDES,
            "sobre_clave": clave,
            "vacacion_id": None,
        }],
        solo_si_tiene_acceso=clave,
    )
