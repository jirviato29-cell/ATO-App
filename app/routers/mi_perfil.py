from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models
from app.config import get_current_user
from app.database import get_db
from app.routers.vacaciones import (
    VacacionResponse,
    _clave_columna,
    _nombres_aprobadores,
    _normalizar,
    _serializar,
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
