"""Quién revisa las solicitudes de vacaciones y día extra, además de Gerencia (= admin).

- RH (RH_CLAVE) marca solo la columna RH, de todas las solicitudes menos las suyas.
- Encargado de módulo: personas (nombre_englobado) con cuentas activas de rol 'encargado';
  sus módulos son los modulo_id de TODAS esas cuentas, entre con la cuenta que entre.
  A eso se suman las asignaciones manuales de ENCARGADOS_MANUALES (por nombre de módulo).
- El módulo de un empleado es el de su cuenta PRINCIPAL (misma regla que saldo y nómina).
- MODULO_DEL_ADMIN (Bodega), un módulo sin encargado o una cuenta principal sin módulo:
  la columna Encargado la marca el admin.
- Nadie marca su propia solicitud: si quien pide es el encargado (o RH), lo marca el admin.

Todo se calcula en cada petición (no se guarda quién es el encargado): si cambia el
encargado de un módulo, aplica solo. Si una clave de aquí no existe o no está activa,
nadie recibe ese permiso.
"""
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models

RH_CLAVE = "A55-EVELYN"
ENCARGADOS_MANUALES = {"A57-ELI": ["Cadenas C."]}
MODULO_DEL_ADMIN = "BO"

ROLES_EMPLEADO = (models.RolEnum.asesor, models.RolEnum.encargado)
NIVELES_REVISOR = ("rh", "encargado")


def normalizar(clave: Optional[str]) -> str:
    return (clave or "").strip().upper()


def clave_de(usuario: models.Usuario) -> str:
    # Igual que /mi-perfil y MENSAJES: englobado o, si no tiene, username
    return normalizar(usuario.nombre_englobado) or normalizar(usuario.username)


def _ids_modulos(db: Session, nombres: Iterable[str]) -> Dict[str, int]:
    nombres = {n for n in nombres if n}
    if not nombres:
        return {}
    return {
        f.nombre: f.id
        for f in db.query(models.Modulo.id, models.Modulo.nombre).filter(models.Modulo.nombre.in_(nombres)).all()
    }


def _claves_activas(db: Session, claves: Iterable[str]) -> Set[str]:
    # Personas con al menos una cuenta activa de empleado con ese englobado
    claves = {normalizar(c) for c in claves if normalizar(c)}
    if not claves:
        return set()
    englobado = func.upper(func.trim(models.Usuario.nombre_englobado))
    return {
        normalizar(f[0])
        for f in db.query(englobado)
        .filter(englobado.in_(claves), models.Usuario.activo == True, models.Usuario.rol.in_(ROLES_EMPLEADO))  # noqa: E712
        .all()
    }


def mapa_encargados(db: Session) -> Dict[str, Set[int]]:
    """clave de persona -> ids de módulo de los que es encargada (sin el módulo del admin)."""
    ids = _ids_modulos(db, [MODULO_DEL_ADMIN, *(n for ns in ENCARGADOS_MANUALES.values() for n in ns)])
    modulo_admin = ids.get(MODULO_DEL_ADMIN)
    mapa: Dict[str, Set[int]] = {}
    for u in (
        db.query(models.Usuario)
        .filter(
            models.Usuario.activo == True,  # noqa: E712
            models.Usuario.rol == models.RolEnum.encargado,
            models.Usuario.modulo_id.isnot(None),
        )
        .all()
    ):
        clave = clave_de(u)
        if clave and u.modulo_id != modulo_admin:
            mapa.setdefault(clave, set()).add(u.modulo_id)
    activas = _claves_activas(db, ENCARGADOS_MANUALES.keys())
    for clave, nombres in ENCARGADOS_MANUALES.items():
        clave = normalizar(clave)
        if clave not in activas:
            continue
        for nombre in nombres:
            mid = ids.get(nombre)
            if mid is not None and mid != modulo_admin:
                mapa.setdefault(clave, set()).add(mid)
    return mapa


def rh_activa(db: Session) -> Optional[str]:
    clave = normalizar(RH_CLAVE)
    return clave if clave in _claves_activas(db, [clave]) else None


def claves_revisoras(db: Session) -> Set[str]:
    """Claves de quienes revisan (RH + encargados): tienen MENSAJES siempre."""
    claves = set(mapa_encargados(db))
    rh = rh_activa(db)
    if rh:
        claves.add(rh)
    return claves


@dataclass
class Alcance:
    clave: str
    rh: bool = False
    modulos: Set[int] = field(default_factory=set)

    @property
    def es_revisor(self) -> bool:
        return self.rh or bool(self.modulos)


def alcance(db: Session, usuario: models.Usuario) -> Alcance:
    # Admin no entra aquí: Gerencia usa /vacaciones/solicitudes. Solo cuentas de empleado.
    if usuario is None or usuario.rol not in ROLES_EMPLEADO or not usuario.activo:
        return Alcance(clave="")
    clave = clave_de(usuario)
    if not clave:
        return Alcance(clave="")
    return Alcance(
        clave=clave,
        rh=clave == rh_activa(db),
        modulos=set(mapa_encargados(db).get(clave, set())),
    )


def es_revisor(db: Session, usuario: models.Usuario) -> bool:
    return alcance(db, usuario).es_revisor


def modulos_principales(db: Session, claves: Iterable[str]) -> Dict[str, Optional[int]]:
    """clave de empleado -> modulo_id de su cuenta principal (None si no tiene)."""
    # Import tardío: vacaciones importa avisos, y avisos importa este archivo
    from app.routers.vacaciones import _cuentas_por_clave, _principal

    claves = {normalizar(c) for c in claves if normalizar(c)}
    cuentas = _cuentas_por_clave(db, claves)
    resultado: Dict[str, Optional[int]] = {}
    for clave in claves:
        principal = _principal(cuentas.get(clave, []))
        resultado[clave] = principal.modulo_id if principal is not None else None
    return resultado


def niveles_que_marca(a: Alcance, clave_empleado: str, modulo_empleado: Optional[int]) -> List[str]:
    """Columnas que esta persona puede marcar en la solicitud de ese empleado."""
    clave_empleado = normalizar(clave_empleado)
    if not a.es_revisor or not clave_empleado or clave_empleado == a.clave:
        return []  # nadie se autoriza a sí mismo
    niveles = []
    if a.rh:
        niveles.append("rh")
    if modulo_empleado is not None and modulo_empleado in a.modulos:
        niveles.append("encargado")
    return niveles


def revisores_de(db: Session, clave_empleado: str) -> List[str]:
    """A quién le llega el aviso de una solicitud: RH + encargado(s) del módulo, sin el solicitante."""
    clave_empleado = normalizar(clave_empleado)
    modulo = modulos_principales(db, [clave_empleado]).get(clave_empleado)
    destinatarios: List[str] = []
    rh = rh_activa(db)
    if rh:
        destinatarios.append(rh)
    if modulo is not None:
        for clave, modulos in sorted(mapa_encargados(db).items()):
            if modulo in modulos and clave not in destinatarios:
                destinatarios.append(clave)
    return [c for c in destinatarios if c != clave_empleado]


def pendientes_de(db: Session, a: Alcance) -> List[dict]:
    """Solicitudes pendientes (vacaciones y día extra) que esta persona puede marcar,
    con los niveles que le tocan y el módulo principal del empleado."""
    if not a.es_revisor:
        return []
    from app.routers.vacaciones import TIPOS_CON_APROBACION

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
    modulos = modulos_principales(db, (v.clave for v in vacaciones))
    filas = []
    for v in vacaciones:
        modulo = modulos.get(normalizar(v.clave))
        niveles = niveles_que_marca(a, v.clave, modulo)
        if niveles:
            filas.append({"vacacion": v, "niveles": niveles, "modulo_id": modulo})
    return filas


def contar_por_revisar(db: Session, a: Alcance) -> int:
    # Pendientes donde a esta persona todavía le falta marcar su columna
    return sum(
        1
        for f in pendientes_de(db, a)
        if any(getattr(f["vacacion"], f"aprobado_{n}") is None for n in f["niveles"])
    )
