"""Avisos automáticos de MENSAJES y quién tiene acceso al módulo.

Regla de oro: un aviso NUNCA rompe lo principal. Las funciones publicas de este
archivo se llaman con BackgroundTasks DESPUES del commit de la solicitud (la
respuesta ya salio), abren su PROPIA sesion y se tragan cualquier error (solo
queda en el log). Reciben valores planos, nunca objetos del ORM.
"""
import logging
import os
from datetime import date, timedelta
from typing import List, Optional, Set

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.database import SessionLocal

log = logging.getLogger("avisos")

# Interruptor en Render (variable de entorno MENSAJES_PARA_TODOS=true): abre MENSAJES a todos
# los empleados (asesor/encargado activos). Apagado por default: solo admin y el piloto de Mi Perfil.
# Se lee en cada llamada; al cambiar la variable, Render reinicia el servicio.
def mensajes_para_todos() -> bool:
    return os.getenv("MENSAJES_PARA_TODOS", "").strip().lower() in ("1", "true", "si", "sí", "yes")

ROLES_EMPLEADO = (models.RolEnum.asesor, models.RolEnum.encargado)

ENLACE_SOLICITUDES = "/mi-perfil?tab=solicitudes"
# Pantalla de RH y encargados (columna RH / Encargado)
ENLACE_REVISAR = "/revisar-solicitudes"


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


def tiene_mensajes(usuario: models.Usuario, db: Optional[Session] = None) -> bool:
    if es_admin(usuario):
        return True
    if mensajes_para_todos():
        if usuario.rol in ROLES_EMPLEADO:
            return True
    elif normalizar(usuario.username) in _usernames_piloto():
        return True
    # RH y encargados (incluidas asignaciones manuales) tienen MENSAJES siempre:
    # ahí les llegan las solicitudes que revisan.
    if db is not None:
        from app.permisos_vacaciones import es_revisor

        return es_revisor(db, usuario)
    return False


def claves_con_mensajes(db: Session) -> Set[str]:
    # Claves de empleado que hoy pueden ver MENSAJES (destinatarios de "a todos" y de avisos)
    from app.permisos_vacaciones import claves_revisoras

    revisores = claves_revisoras(db)
    q = db.query(models.Usuario).filter(models.Usuario.activo == True)  # noqa: E712
    if mensajes_para_todos():
        q = q.filter(models.Usuario.rol.in_(ROLES_EMPLEADO))
    else:
        pilotos = _usernames_piloto()
        if not pilotos:
            return revisores
        q = q.filter(func.upper(func.trim(models.Usuario.username)).in_(pilotos))
    return {c for c in (clave_de(u) for u in q.all() if not es_admin(u)) if c} | revisores


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


def _guardar_con_revisores(fila_admin: dict) -> None:
    """La fila de administración (igual que siempre) + una copia personal para RH y para el
    encargado del módulo del empleado, con enlace a su pantalla. Si calcular a quién le toca
    falla, la fila de administración se guarda de todos modos."""
    db = SessionLocal()
    try:
        filas = [fila_admin]
        try:
            from app.permisos_vacaciones import revisores_de

            for clave in revisores_de(db, fila_admin["sobre_clave"]):
                filas.append({**fila_admin, "para_clave": clave, "enlace": ENLACE_REVISAR})
        except Exception:
            log.exception("No se pudo calcular RH/encargado del aviso; solo va a administración")
            db.rollback()
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


# --------------------------- avisos públicos ---------------------------

@_seguro
def solicitud_nueva(clave: str, tipo: str, inicio: date, fin: date, dias, vacacion_id: int) -> None:
    clave = normalizar(clave)
    _guardar_con_revisores({
        "para_clave": None,
        "tipo": "solicitud_nueva",
        "texto": f"{clave} solicitó {_que(tipo)} {_fechas(inicio, fin, dias)}",
        "enlace": ENLACE_SOLICITUDES,
        "sobre_clave": clave,
        "vacacion_id": vacacion_id,
    })


@_seguro
def solicitud_cancelada(clave: str, tipo: str, inicio: date, fin: date, dias, vacacion_id: int) -> None:
    clave = normalizar(clave)
    _guardar_con_revisores({
        "para_clave": None,
        "tipo": "solicitud_cancelada",
        "texto": f"{clave} canceló su solicitud de {_que(tipo)} {_fechas(inicio, fin, dias)}",
        "enlace": ENLACE_SOLICITUDES,
        "sobre_clave": clave,
        "vacacion_id": vacacion_id,
    })


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


# ------------------- Flujo con cobertura (RH + Encargado + Gerencia + cubre) -------------------

def _del(inicio: date, fin: date) -> str:
    return f"del {_dm(inicio)}" if inicio == fin else f"del {_dm(inicio)} al {_dm(fin)}"


@_seguro
def encargado_aprobo(clave: str, inicio: date, fin: date, vacacion_id: int) -> None:
    clave = normalizar(clave)
    _guardar(
        [{
            "para_clave": clave,
            "tipo": "encargado_aprobo",
            "texto": f"El encargado aprobó tu solicitud {_del(inicio, fin)}",
            "enlace": ENLACE_SOLICITUDES,
            "sobre_clave": clave,
            "vacacion_id": vacacion_id,
        }],
        solo_si_tiene_acceso=clave,
    )


@_seguro
def cubrir_solicitud(cubre: str, clave: str, inicio: date, fin: date, vacacion_id: int) -> None:
    # Lleva botones CONFIRMO / NO PUEDO (los resuelve GET /mensajes/avisos); no lleva enlace
    cubre, clave = normalizar(cubre), normalizar(clave)
    _guardar(
        [{
            "para_clave": cubre,
            "tipo": "cubrir_solicitud",
            "texto": f"¿Confirmas que vas a cubrir a {clave} {_del(inicio, fin)}?",
            "enlace": None,
            "sobre_clave": clave,
            "vacacion_id": vacacion_id,
        }],
        solo_si_tiene_acceso=cubre,
    )


@_seguro
def cubre_no_puede(cubre: str, clave: str, inicio: date, fin: date, vacacion_id: int) -> None:
    cubre, clave = normalizar(cubre), normalizar(clave)
    _guardar([{
        "para_clave": None,
        "tipo": "cubre_no_puede",
        "texto": f"{cubre} no puede cubrir a {clave} {_del(inicio, fin)}; elige a otra persona",
        "enlace": ENLACE_SOLICITUDES,
        "sobre_clave": clave,
        "vacacion_id": vacacion_id,
    }])


@_seguro
def solicitud_aprobada(clave: str, tipo: str, inicio: date, fin: date, vacacion_id: int) -> None:
    # Los 4 en verde: RH, Encargado, Gerencia y quien cubre confirmó
    clave = normalizar(clave)
    if tipo == "dia_extra":
        texto = (
            f"Tu día extra del {_dm(inicio)} ha sido aprobado" if inicio == fin
            else f"Tus días extra {_del(inicio, fin)} han sido aprobados"
        )
    else:
        texto = f"Tus vacaciones {_del(inicio, fin)} han sido aprobadas"
    _guardar(
        [{
            "para_clave": clave,
            "tipo": "solicitud_autorizada",
            "texto": texto,
            "enlace": ENLACE_SOLICITUDES,
            "sobre_clave": clave,
            "vacacion_id": vacacion_id,
        }],
        solo_si_tiene_acceso=clave,
    )


@_seguro
def cobertura_cancelada(cubre: str, clave: str, inicio: date, fin: date, vacacion_id: int) -> None:
    cubre, clave = normalizar(cubre), normalizar(clave)
    _guardar(
        [{
            "para_clave": cubre,
            "tipo": "cobertura_cancelada",
            "texto": f"Ya no tienes que cubrir a {clave} {_del(inicio, fin)}",
            "enlace": None,
            "sobre_clave": clave,
            "vacacion_id": vacacion_id,
        }],
        solo_si_tiene_acceso=cubre,
    )
