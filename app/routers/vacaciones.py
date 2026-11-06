from typing import List, Optional
from datetime import date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.utilidades import verificar_rol_requerido

router = APIRouter()

# Solo lectura. La tabla vacaciones vive en la BD; aqui no se crea, edita ni
# borra nada.


class VacacionResponse(BaseModel):
    id: int
    clave: str
    fecha_inicio: date
    fecha_fin: date
    dias: Optional[float] = None
    estado: Optional[str] = None
    origen: Optional[str] = None
    nota: Optional[str] = None

    class Config:
        from_attributes = True


@router.get("", response_model=List[VacacionResponse])
@router.get("/", response_model=List[VacacionResponse], include_in_schema=False)
def listar_vacaciones(
    clave: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    _admin: models.Usuario = Depends(verificar_rol_requerido(models.RolEnum.admin)),
):
    clave_norm = clave.strip().upper()
    return (
        db.query(models.Vacacion)
        .filter(func.upper(func.trim(models.Vacacion.clave)) == clave_norm)
        .order_by(models.Vacacion.fecha_inicio, models.Vacacion.id)
        .all()
    )
