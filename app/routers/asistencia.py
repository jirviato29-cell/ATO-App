import base64
import os
import traceback
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app import models, schemas
from app.config import get_current_user
from app.database import get_db

ZONA = ZoneInfo("America/Mexico_City")


def _hora_mx_str(dt: datetime) -> str:
    """datetime (cualquier tz) → '9:20 a.m.' en zona México."""
    dt_mx = dt.astimezone(ZONA)
    hora = str(dt_mx.hour % 12 or 12)
    minuto = f"{dt_mx.minute:02d}"
    sufijo = "a.m." if dt_mx.hour < 12 else "p.m."
    return f"{hora}:{minuto} {sufijo}"


router = APIRouter(prefix="/asistencia", tags=["Asistencia"])
modulos_router = APIRouter(prefix="/modulos", tags=["Asistencia"])
promotores_router = APIRouter(prefix="/promotores", tags=["Promotores Cadenas"])


# ── Supabase admin client ─────────────────────────────────────────────────────

def _supabase_admin():
    from supabase import create_client
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise HTTPException(500, "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY no configurados")
    return create_client(url, key)


# ── Helper: agrupar registros por (usuario, fecha) ───────────────────────────

def _agrupar(registros: list, include_user: bool = False) -> List[schemas.AsistenciaResumenDia]:
    dias: dict = {}
    for r in registros:
        key = (r.usuario_id, r.fecha) if include_user else (0, r.fecha)
        if key not in dias:
            dias[key] = {"entrada": None, "salida": None}
        dias[key][r.tipo] = r

    resultado = []
    for key in sorted(dias):
        data = dias[key]
        ent = data.get("entrada")
        sal = data.get("salida")

        horas = 0.0
        if ent and sal and ent.hora and sal.hora:
            from datetime import timezone as _utc
            def _to_utc(dt):
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=_utc.utc)
                return dt.astimezone(_utc.utc)
            delta = _to_utc(sal.hora) - _to_utc(ent.hora)
            horas = max(0.0, delta.total_seconds() / 3600)

        ref = ent or sal
        modulo_nombre = None
        if ref and ref.modulo_rel:
            modulo_nombre = ref.modulo_rel.nombre

        lugar_trabajo = None
        if ref and ref.usuario:
            lugar_trabajo = ref.usuario.lugar_trabajo

        resultado.append(schemas.AsistenciaResumenDia(
            fecha=(ent or sal).fecha,
            entrada=ent.hora if ent else None,
            salida=sal.hora if sal else None,
            entrada_id=ent.id if ent else None,
            salida_id=sal.id if sal else None,
            horas_trabajadas=round(horas, 2),
            foto_entrada_url=ent.foto_url if ent else None,
            foto_salida_url=sal.foto_url if sal else None,
            dentro_de_zona_entrada=ent.dentro_de_zona if ent else None,
            dentro_de_zona_salida=sal.dentro_de_zona if sal else None,
            distancia_metros_entrada=ent.distancia_metros if ent else None,
            distancia_metros_salida=sal.distancia_metros if sal else None,
            username=ent.username if ent else (sal.username if sal else None),
            modulo_id=ent.modulo_id if ent else (sal.modulo_id if sal else None),
            modulo_nombre=modulo_nombre,
            lugar_trabajo=lugar_trabajo,
        ))
    return resultado


# ── Endpoints de asistencia ───────────────────────────────────────────────────

@router.post("/check", response_model=schemas.AsistenciaResponse)
def check_asistencia(
    body: schemas.AsistenciaCreate,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in ("asesor", "encargado"):
        raise HTTPException(403, "Solo asesores y encargados pueden registrar asistencia")

    modulo = db.query(models.Modulo).filter(models.Modulo.id == current_user.modulo_id).first()
    if not modulo:
        raise HTTPException(404, "El usuario no tiene módulo asignado")

    fecha_actual = datetime.now(ZONA).date()

    if body.tipo == "entrada":
        existente = db.query(models.Asistencia).filter(
            models.Asistencia.usuario_id == current_user.id,
            models.Asistencia.fecha == fecha_actual,
            models.Asistencia.tipo == "entrada",
        ).first()
        if existente:
            raise HTTPException(400, detail={
                "codigo": "ENTRADA_DUPLICADA",
                "mensaje": f"Ya tienes registrado tu CHECK-IN de hoy a las {_hora_mx_str(existente.hora)}. Si crees que es un error, reporta a administración.",
            })
    else:
        entrada_hoy = db.query(models.Asistencia).filter(
            models.Asistencia.usuario_id == current_user.id,
            models.Asistencia.fecha == fecha_actual,
            models.Asistencia.tipo == "entrada",
        ).first()
        if not entrada_hoy:
            db.add(models.NotificacionAsistencia(
                asistencia_id=None,
                usuario_id=current_user.id,
                username=current_user.username,
                modulo_id=current_user.modulo_id,
                mensaje=(
                    f"🚨 SIN CHECK-IN: {current_user.username} intentó hacer check-out "
                    f"sin haber registrado entrada el día {fecha_actual}"
                ),
                distancia_metros=None,
            ))
            db.commit()
            raise HTTPException(400, detail={
                "codigo": "SIN_CHECKIN",
                "mensaje": "No tienes registrado tu CHECK-IN de hoy. Por favor reporta esto a administración para que lo revisen.",
            })
        salida_existente = db.query(models.Asistencia).filter(
            models.Asistencia.usuario_id == current_user.id,
            models.Asistencia.fecha == fecha_actual,
            models.Asistencia.tipo == "salida",
        ).first()
        if salida_existente:
            raise HTTPException(400, detail={
                "codigo": "SALIDA_DUPLICADA",
                "mensaje": f"Ya tienes registrado tu CHECK-OUT de hoy a las {_hora_mx_str(salida_existente.hora)}. Si crees que es un error, reporta a administración.",
            })

    # Determinar si es promotor de Cadenas (módulo contiene "Cadenas" y username empieza con "C")
    is_cadenas_promotor = (
        "Cadenas" in (modulo.nombre or "")
        and current_user.username.startswith("C")
    )

    dentro_de_zona = True
    distancia = None

    if is_cadenas_promotor:
        if (
            current_user.latitud_promotor is not None
            and current_user.longitud_promotor is not None
        ):
            from app.utils.geo import distancia_metros
            distancia = distancia_metros(
                body.latitud, body.longitud,
                current_user.latitud_promotor, current_user.longitud_promotor,
            )
            dentro_de_zona = distancia <= (current_user.radio_metros_promotor or 100)
        # Si no tiene coords configuradas: dentro_de_zona = True por default
    elif modulo.latitud is not None and modulo.longitud is not None:
        from app.utils.geo import distancia_metros
        distancia = distancia_metros(body.latitud, body.longitud, modulo.latitud, modulo.longitud)
        dentro_de_zona = distancia <= (modulo.radio_metros or 100)

    # Subir foto a Supabase Storage
    supabase = _supabase_admin()
    ts = int(datetime.now(ZONA).timestamp())
    fecha_str = fecha_actual.isoformat()
    filename = f"{current_user.username}_{fecha_str}_{body.tipo}_{ts}.jpg"

    raw_b64 = body.foto_base64.strip()
    if "," in raw_b64:
        raw_b64 = raw_b64.split(",", 1)[1].strip()

    if not raw_b64:
        raise HTTPException(400, detail={
            "codigo": "FOTO_INVALIDA",
            "mensaje": "La foto no se capturó correctamente (imagen vacía). Intenta de nuevo.",
        })

    try:
        img_bytes = base64.b64decode(raw_b64)
    except Exception:
        raise HTTPException(400, detail={
            "codigo": "FOTO_INVALIDA",
            "mensaje": "La foto no es válida (datos corruptos). Intenta de nuevo.",
        })

    if len(img_bytes) < 3072:
        raise HTTPException(400, detail={
            "codigo": "FOTO_INVALIDA",
            "mensaje": "La foto no se capturó correctamente (imagen demasiado pequeña). Intenta de nuevo.",
        })

    supabase.storage.from_("asistencia-fotos").upload(
        path=filename,
        file=img_bytes,
        file_options={"content-type": "image/jpeg", "upsert": "true"},
    )
    foto_url = f"{os.getenv('SUPABASE_URL')}/storage/v1/object/public/asistencia-fotos/{filename}"

    ahora = datetime.now(ZONA)
    registro = models.Asistencia(
        usuario_id=current_user.id,
        username=current_user.username,
        modulo_id=current_user.modulo_id,
        fecha=fecha_actual,
        tipo=body.tipo,
        hora=ahora,
        latitud=body.latitud,
        longitud=body.longitud,
        foto_url=foto_url,
        dentro_de_zona=dentro_de_zona,
        distancia_metros=distancia,
    )
    db.add(registro)

    db.flush()

    if not dentro_de_zona:
        notif = models.NotificacionAsistencia(
            asistencia_id=registro.id,
            usuario_id=current_user.id,
            username=current_user.username,
            modulo_id=current_user.modulo_id,
            mensaje=(
                f"{current_user.username} hizo {body.tipo} fuera de zona "
                f"— a {int(distancia)} metros del punto de referencia"
            ),
            distancia_metros=distancia,
        )
        db.add(notif)

    db.commit()
    db.refresh(registro)
    return registro


@router.get("/mi-historial", response_model=List[schemas.AsistenciaResumenDia])
def mi_historial(
    desde: date = Query(...),
    hasta: date = Query(...),
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in ("asesor", "encargado"):
        raise HTTPException(403, "Solo asesores y encargados")

    registros = (
        db.query(models.Asistencia)
        .filter(
            models.Asistencia.usuario_id == current_user.id,
            models.Asistencia.fecha >= desde,
            models.Asistencia.fecha <= hasta,
        )
        .all()
    )
    return _agrupar(registros)


@router.get("/admin", response_model=List[schemas.AsistenciaResumenDia])
def admin_historial(
    usuario_id: Optional[int] = None,
    modulo_id: Optional[int] = None,
    desde: Optional[date] = None,
    hasta: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in ("admin", "direccion"):
        raise HTTPException(403, "Solo admin y dirección")

    q = db.query(models.Asistencia)
    if usuario_id:
        q = q.filter(models.Asistencia.usuario_id == usuario_id)
    if modulo_id:
        q = q.filter(models.Asistencia.modulo_id == modulo_id)
    if desde:
        q = q.filter(models.Asistencia.fecha >= desde)
    if hasta:
        q = q.filter(models.Asistencia.fecha <= hasta)

    return _agrupar(q.all(), include_user=True)


PRIMER_CICLO = date(2026, 5, 11)
_MESES = ["ene", "feb", "mar", "abr", "may", "jun",
          "jul", "ago", "sep", "oct", "nov", "dic"]


@router.get("/ciclos", response_model=List[schemas.CicloSemana])
def listar_ciclos(
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in ("admin", "direccion"):
        raise HTTPException(403, "Solo admin y dirección")

    hoy = datetime.now(ZONA).date()
    lunes_actual = hoy - timedelta(days=hoy.weekday())

    ciclos = []
    lunes = PRIMER_CICLO
    while lunes <= lunes_actual:
        domingo = lunes + timedelta(days=6)
        label = (
            f"lun {lunes.day} de {_MESES[lunes.month - 1]}"
            f" — dom {domingo.day} de {_MESES[domingo.month - 1]}"
        )
        ciclos.append(schemas.CicloSemana(inicio=lunes, fin=domingo, label=label))
        lunes += timedelta(days=7)

    ciclos.reverse()
    return ciclos


@router.get("/acumulado-semanal", response_model=List[schemas.EmpleadoAcumuladoSemanal])
def acumulado_semanal(
    ciclo: date = Query(...),
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in ("admin", "direccion"):
        raise HTTPException(403, "Solo admin y dirección")

    if ciclo.weekday() != 0:
        raise HTTPException(400, "ciclo debe ser un lunes (weekday=0)")

    try:
        lunes = ciclo
        domingo = ciclo + timedelta(days=6)

        registros = (
            db.query(models.Asistencia)
            .filter(
                models.Asistencia.fecha >= lunes,
                models.Asistencia.fecha <= domingo,
            )
            .all()
        )
        resumenes = _agrupar(registros, include_user=True)

        por_username: Dict[str, Dict] = {}
        for r in resumenes:
            if r.username not in por_username:
                por_username[r.username] = {}
            por_username[r.username][r.fecha] = r

        empleados = (
            db.query(models.Usuario)
            .filter(models.Usuario.activo == True)  # noqa: E712
            .filter(models.Usuario.rol.in_(["asesor", "encargado"]))
            .order_by(models.Usuario.username)
            .all()
        )

        ciclo_anterior = lunes - timedelta(days=7)
        jornadas = (
            db.query(models.JornadaAsistencia)
            .filter(models.JornadaAsistencia.ciclo_inicio.in_([lunes, ciclo_anterior]))
            .all()
        )
        jornada_map: Dict[tuple, float] = {
            (j.usuario_id, j.ciclo_inicio): float(j.horas) for j in jornadas
        }

        # ── Dias de descanso, propagados por nombre_englobado ────────────────
        grupos: Dict[str, List[int]] = {}
        grupo_de: Dict[int, str] = {}
        for emp in empleados:
            eng = (emp.nombre_englobado or "").strip().upper()
            clave = eng if eng else f"__solo__{emp.id}"
            grupos.setdefault(clave, []).append(emp.id)
            grupo_de[emp.id] = clave

        ids_empleados = [e.id for e in empleados]
        descansos_sem = db.query(models.DiaDescanso).filter(
            models.DiaDescanso.usuario_id.in_(ids_empleados),
            models.DiaDescanso.fecha >= lunes,
            models.DiaDescanso.fecha <= domingo,
            models.DiaDescanso.cancelado_at.is_(None),
        ).all() if ids_empleados else []

        descansos_por_grupo: Dict[str, set] = {}
        for d in descansos_sem:
            clave = grupo_de.get(d.usuario_id)
            if clave:
                descansos_por_grupo.setdefault(clave, set()).add(d.fecha.isoformat())

        resultado = []
        for emp in empleados:
            dias_emp = por_username.get(emp.username, {})
            dias: Dict[str, Optional[schemas.DiaResumen]] = {}
            total_horas = 0.0

            for i in range(7):
                dia = lunes + timedelta(days=i)
                r = dias_emp.get(dia)
                if r is not None:
                    dias[dia.isoformat()] = schemas.DiaResumen(
                        entrada=r.entrada,
                        salida=r.salida,
                        horas=r.horas_trabajadas,
                    )
                    total_horas += r.horas_trabajadas
                else:
                    dias[dia.isoformat()] = None

            total_horas = round(total_horas, 2)

            jornada = jornada_map.get((emp.id, lunes))
            if jornada is None:
                jornada = jornada_map.get((emp.id, ciclo_anterior))

            horas_extra = round(total_horas - jornada, 2) if jornada is not None else None

            resultado.append(schemas.EmpleadoAcumuladoSemanal(
                usuario_id=emp.id,
                username=emp.username,
                nombre_completo=emp.nombre_completo or emp.username,
                nombre_englobado=emp.nombre_englobado,
                dias=dias,
                total_horas=total_horas,
                jornada=jornada,
                horas_extra=horas_extra,
                jornada_fija=float(emp.jornada_fija) if emp.jornada_fija is not None else 0.0,
                sueldo_base=float(emp.sueldo_base) if emp.sueldo_base is not None else 0.0,
                descansos=sorted(descansos_por_grupo.get(grupo_de.get(emp.id, ""), set())),
            ))

        return resultado

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"{type(e).__name__}: {str(e)} | {traceback.format_exc()}",
        )


@router.put("/jornada")
def upsert_jornada(
    body: schemas.JornadaUpsert,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in ("admin", "direccion"):
        raise HTTPException(403, "Solo admin y dirección")

    existente = (
        db.query(models.JornadaAsistencia)
        .filter(
            models.JornadaAsistencia.usuario_id == body.usuario_id,
            models.JornadaAsistencia.ciclo_inicio == body.ciclo,
        )
        .first()
    )

    if existente:
        existente.horas = body.horas
        existente.updated_at = datetime.now(ZONA)
    else:
        db.add(models.JornadaAsistencia(
            usuario_id=body.usuario_id,
            ciclo_inicio=body.ciclo,
            horas=body.horas,
        ))

    db.commit()
    return {"ok": True}


@router.get("/notificaciones", response_model=List[schemas.NotificacionResponse])
def listar_notificaciones(
    solo_no_leidas: bool = True,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in ("admin", "direccion", "encargado"):
        raise HTTPException(403, "Sin permiso para ver notificaciones")

    q = db.query(models.NotificacionAsistencia)
    if current_user.rol == "encargado":
        q = q.filter(models.NotificacionAsistencia.modulo_id == current_user.modulo_id)
    if solo_no_leidas:
        q = q.filter(models.NotificacionAsistencia.leida == False)  # noqa: E712
    return q.order_by(models.NotificacionAsistencia.creada_at.desc()).all()


@router.put("/notificaciones/{notif_id}/marcar-leida")
def marcar_leida(
    notif_id: int,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in ("admin", "direccion", "encargado"):
        raise HTTPException(403, "Sin permiso para marcar notificaciones")

    notif = db.query(models.NotificacionAsistencia).filter(
        models.NotificacionAsistencia.id == notif_id
    ).first()
    if not notif:
        raise HTTPException(404, "Notificación no encontrada")

    if current_user.rol == "encargado" and notif.modulo_id != current_user.modulo_id:
        raise HTTPException(403, "Solo puedes marcar notificaciones de tu módulo")

    notif.leida = True
    db.commit()
    return {"ok": True}


# ── Endpoints de módulos (ubicación) ─────────────────────────────────────────

@modulos_router.put("/{modulo_id}/ubicacion")
def actualizar_ubicacion_modulo(
    modulo_id: int,
    body: schemas.ModuloUbicacionUpdate,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol != "admin":
        raise HTTPException(403, "Solo admin")

    modulo = db.query(models.Modulo).filter(models.Modulo.id == modulo_id).first()
    if not modulo:
        raise HTTPException(404, "Módulo no encontrado")

    modulo.latitud = body.latitud
    modulo.longitud = body.longitud
    modulo.radio_metros = body.radio_metros
    db.commit()
    return {"ok": True}


@modulos_router.get("/con-ubicacion", response_model=List[schemas.ModuloConUbicacion])
def modulos_con_ubicacion(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in ("admin", "direccion"):
        raise HTTPException(403, "Solo admin y dirección")
    return db.query(models.Modulo).all()


@modulos_router.get("/{modulo_id}/encargado", response_model=schemas.EncargadoResponse)
def encargado_del_modulo(
    modulo_id: int,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    encargado = (
        db.query(models.Usuario)
        .filter(
            models.Usuario.modulo_id == modulo_id,
            models.Usuario.rol == "encargado",
            models.Usuario.activo == True,  # noqa: E712
        )
        .first()
    )
    if not encargado:
        raise HTTPException(404, "Este módulo no tiene encargado asignado")
    return schemas.EncargadoResponse(
        modulo_id=modulo_id,
        usuario_id=encargado.id,
        username=encargado.username,
        nombre_completo=encargado.nombre_completo,
    )


# ── Endpoints de promotores Cadenas (ubicación por promotor) ─────────────────

@promotores_router.get("/con-ubicacion", response_model=List[schemas.PromotorConUbicacion])
def promotores_con_ubicacion(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in ("admin", "direccion"):
        raise HTTPException(403, "Solo admin y dirección")

    return (
        db.query(models.Usuario)
        .join(models.Modulo, models.Usuario.modulo_id == models.Modulo.id)
        .filter(
            models.Usuario.username.like("C%"),
            models.Modulo.nombre.contains("Cadenas"),
        )
        .all()
    )


@promotores_router.put("/{usuario_id}/ubicacion")
def actualizar_ubicacion_promotor(
    usuario_id: int,
    body: schemas.PromotorUbicacionUpdate,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol != "admin":
        raise HTTPException(403, "Solo admin")

    usuario = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if not usuario:
        raise HTTPException(404, "Usuario no encontrado")

    modulo = db.query(models.Modulo).filter(models.Modulo.id == usuario.modulo_id).first()
    if not modulo or "Cadenas" not in (modulo.nombre or ""):
        raise HTTPException(400, "El usuario no pertenece al módulo Cadenas")

    if not usuario.username.startswith("C"):
        raise HTTPException(400, "El username del usuario no empieza con C")

    usuario.lugar_trabajo = body.lugar_trabajo
    usuario.latitud_promotor = body.latitud_promotor
    usuario.longitud_promotor = body.longitud_promotor
    usuario.radio_metros_promotor = body.radio_metros_promotor
    db.commit()
    return {"ok": True}


# ── Mi semana (detalle lunes→domingo del usuario logueado) ────────────────────

_DIAS_SEMANA = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]


def _hm_mx(dt: datetime) -> str:
    """datetime (UTC o naive-asumido-UTC) → 'HH:MM' (24h) en zona México."""
    from datetime import timezone as _utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_utc.utc)
    return dt.astimezone(ZONA).strftime("%H:%M")


@router.get("/mi-semana", response_model=schemas.SemanaDetalle)
def mi_semana(
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if abs(offset) > 52:
        raise HTTPException(status_code=400, detail="Rango de semanas fuera de limite")

    hoy = datetime.now(ZONA).date()
    lunes = hoy - timedelta(days=hoy.weekday()) + timedelta(weeks=offset)
    domingo = lunes + timedelta(days=6)

    registros = (
        db.query(models.Asistencia)
        .filter(
            models.Asistencia.usuario_id == current_user.id,
            models.Asistencia.fecha >= lunes,
            models.Asistencia.fecha <= domingo,
        )
        .all()
    )

    # Reutiliza el agrupado/cálculo de horas existente; indexa por fecha.
    resumen_por_fecha = {r.fecha: r for r in _agrupar(registros)}

    dias = []
    for i in range(7):
        fecha = lunes + timedelta(days=i)
        r = resumen_por_fecha.get(fecha)

        entrada = None
        salida = None
        horas = None
        if r is not None:
            if r.entrada is not None:
                entrada = schemas.MarcaDia(
                    hora=_hm_mx(r.entrada),
                    foto_url=r.foto_entrada_url,
                    dentro_de_zona=r.dentro_de_zona_entrada,
                )
            if r.salida is not None:
                salida = schemas.MarcaDia(
                    hora=_hm_mx(r.salida),
                    foto_url=r.foto_salida_url,
                    dentro_de_zona=r.dentro_de_zona_salida,
                )

        if entrada is not None and salida is not None:
            estado = "completo"
            horas = round(r.horas_trabajadas, 1)
        elif entrada is not None:
            estado = "en_turno"
        elif fecha < hoy:
            estado = "falta"
        else:
            estado = "pendiente"

        dias.append(schemas.DiaSemanaDetalle(
            fecha=fecha,
            dia_semana=_DIAS_SEMANA[i],
            entrada=entrada,
            salida=salida,
            horas=horas,
            estado=estado,
        ))

    return schemas.SemanaDetalle(
        semana_inicio=lunes,
        semana_fin=domingo,
        dias=dias,
    )


# ── Anomalías de asistencia del día (para admin/dirección) ───────────────────

@router.get("/anomalias")
def asistencia_anomalias(
    fecha: date,
    modulo_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in ("admin", "direccion"):
        raise HTTPException(403, "Solo admin y dirección")

    # Universo esperado: usuarios activos que marcan asistencia (asesor/encargado).
    base_q = db.query(models.Usuario).filter(
        models.Usuario.activo == True,
        models.Usuario.rol.in_([models.RolEnum.asesor, models.RolEnum.encargado]),
    )

    if modulo_id:
        # Las cuentas de una persona englobada pueden estar en módulos distintos.
        # 1) nombre_englobado con AL MENOS una cuenta en el módulo pedido.
        englobados_en_modulo = [
            n for (n,) in base_q.with_entities(models.Usuario.nombre_englobado)
            .filter(
                models.Usuario.modulo_id == modulo_id,
                models.Usuario.nombre_englobado.isnot(None),
                models.Usuario.nombre_englobado != "",
            )
            .distinct()
            .all()
        ]
        # 2) TODAS las cuentas de esas personas (aunque estén en otros módulos)
        #    + las cuentas individuales de ese módulo (capturadas por modulo_id).
        cond = models.Usuario.modulo_id == modulo_id
        if englobados_en_modulo:
            cond = cond | models.Usuario.nombre_englobado.in_(englobados_en_modulo)
        usuarios = base_q.filter(cond).all()
    else:
        usuarios = base_q.all()

    # Asistencia del día: TODOS los registros de la fecha SIN filtrar por módulo,
    # porque una persona puede haber marcado con una cuenta de otro módulo.
    registros = db.query(models.Asistencia).filter(
        models.Asistencia.fecha == fecha
    ).all()

    # Agrupar registros por usuario_id (mismo criterio que _agrupar, indexado por usuario)
    por_usuario: Dict[int, dict] = {}
    for r in registros:
        if r.usuario_id not in por_usuario:
            por_usuario[r.usuario_id] = {"entrada": None, "salida": None}
        por_usuario[r.usuario_id][r.tipo] = r

    # Horas con la MISMA lógica de _agrupar
    from datetime import timezone as _utc

    def _to_utc(dt):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=_utc.utc)
        return dt.astimezone(_utc.utc)

    def _horas(ent, sal):
        if ent and sal and ent.hora and sal.hora:
            delta = _to_utc(sal.hora) - _to_utc(ent.hora)
            return max(0.0, delta.total_seconds() / 3600)
        return 0.0

    # Agrupar el universo por PERSONA (nombre_englobado, o id:<id> si no englobada).
    personas: Dict[str, List[models.Usuario]] = {}
    orden: List[str] = []
    for u in usuarios:
        clave = u.nombre_englobado if (u.nombre_englobado not in (None, "")) else f"id:{u.id}"
        if clave not in personas:
            personas[clave] = []
            orden.append(clave)
        personas[clave].append(u)

    sin_movimiento = []
    falta_checkin = []
    falta_checkout = []
    menos_de_una_hora = []
    canonicos: set[int] = set()

    for clave in orden:
        cuentas = personas[clave]
        cuenta_canonica = min(cuentas, key=lambda c: c.id)
        usuario_id_canonico = cuenta_canonica.id
        canonicos.add(usuario_id_canonico)

        # Nombre a mostrar: englobado si existe, si no el username de la cuenta.
        if cuenta_canonica.nombre_englobado not in (None, ""):
            nombre_mostrar = cuenta_canonica.nombre_englobado
        else:
            nombre_mostrar = cuenta_canonica.username

        # Módulo: el de la cuenta canónica, o el primero disponible del grupo.
        cuenta_mod = cuenta_canonica
        if not cuenta_mod.modulo_id:
            cuenta_mod = next((c for c in cuentas if c.modulo_id), cuenta_canonica)
        modulo_nombre = cuenta_mod.modulo.nombre if cuenta_mod.modulo else None

        base = {
            "usuario_id": usuario_id_canonico,
            "username": nombre_mostrar,
            "nombre_completo": cuenta_canonica.nombre_completo,
            "modulo_id": cuenta_mod.modulo_id,
            "modulo_nombre": modulo_nombre,
        }

        # Consolidar asistencia: juntar entradas/salidas de TODAS las cuentas.
        entradas = []
        salidas = []
        for c in cuentas:
            data = por_usuario.get(c.id)
            if not data:
                continue
            ent = data.get("entrada")
            sal = data.get("salida")
            if ent and ent.hora:
                entradas.append(ent)
            if sal and sal.hora:
                salidas.append(sal)

        mejor_entrada = min(entradas, key=lambda r: _to_utc(r.hora)) if entradas else None
        mejor_salida = max(salidas, key=lambda r: _to_utc(r.hora)) if salidas else None

        # Clasificación única por persona.
        if mejor_entrada and mejor_salida:
            horas = _horas(mejor_entrada, mejor_salida)
            if horas < 1:
                menos_de_una_hora.append({
                    **base,
                    "entrada": mejor_entrada.hora,
                    "salida": mejor_salida.hora,
                    "horas_trabajadas": round(horas, 2),
                })
            # entrada + salida con horas >= 1 NO es anomalía: se omite
        elif mejor_entrada and not mejor_salida:
            falta_checkout.append({**base, "entrada": mejor_entrada.hora})
        elif mejor_salida and not mejor_entrada:
            falta_checkin.append({**base, "salida": mejor_salida.hora})
        else:
            sin_movimiento.append(base)

    # Justificaciones ya guardadas de esa fecha, cruzando por el usuario_id canónico.
    justificaciones = {}
    if canonicos:
        jq = db.query(models.JustificacionAsistencia).filter(
            models.JustificacionAsistencia.fecha == fecha,
            models.JustificacionAsistencia.usuario_id.in_(canonicos),
        )
        justificaciones = {
            j.usuario_id: {"estado": j.estado, "nota": j.nota}
            for j in jq.all()
        }

    return {
        "fecha": fecha,
        "sin_movimiento": sin_movimiento,
        "falta_checkin": falta_checkin,
        "falta_checkout": falta_checkout,
        "menos_de_una_hora": menos_de_una_hora,
        "justificaciones": justificaciones,
    }


# ── Justificaciones de asistencia (admin/dirección) ──────────────────────────

@router.post("/justificacion")
def guardar_justificacion(
    body: schemas.JustificacionCreate,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in ("admin", "direccion"):
        raise HTTPException(403, "Solo admin y dirección")

    ahora = datetime.now(ZONA)

    # Upsert por usuario_id + fecha
    registro = db.query(models.JustificacionAsistencia).filter(
        models.JustificacionAsistencia.usuario_id == body.usuario_id,
        models.JustificacionAsistencia.fecha == body.fecha,
    ).first()

    if registro:
        registro.estado = body.estado
        registro.nota = body.nota
        registro.creado_por = current_user.id
        registro.actualizado_en = ahora
    else:
        registro = models.JustificacionAsistencia(
            usuario_id=body.usuario_id,
            fecha=body.fecha,
            estado=body.estado,
            nota=body.nota,
            creado_por=current_user.id,
        )
        db.add(registro)

    db.commit()
    db.refresh(registro)

    return {
        "id": registro.id,
        "usuario_id": registro.usuario_id,
        "fecha": registro.fecha,
        "estado": registro.estado,
        "nota": registro.nota,
        "creado_por": registro.creado_por,
        "creado_en": registro.creado_en,
        "actualizado_en": registro.actualizado_en,
    }


@router.delete("/justificacion")
def borrar_justificacion(
    usuario_id: int,
    fecha: date,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in ("admin", "direccion"):
        raise HTTPException(403, "Solo admin y dirección")

    borradas = db.query(models.JustificacionAsistencia).filter(
        models.JustificacionAsistencia.usuario_id == usuario_id,
        models.JustificacionAsistencia.fecha == fecha,
    ).delete()
    db.commit()

    return {"ok": True, "borradas": borradas}


# ── Solicitudes de corrección de asistencia ──────────────────────────────────

@router.post("/solicitud-correccion", response_model=schemas.SolicitudCorreccionResponse)
def crear_solicitud_correccion(
    body: schemas.SolicitudCorreccionCreate,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in ("asesor", "encargado"):
        raise HTTPException(403, "Solo asesores y encargados pueden solicitar correcciones")

    registro = db.query(models.Asistencia).filter(
        models.Asistencia.id == body.asistencia_id,
    ).first()
    if not registro or registro.usuario_id != current_user.id:
        raise HTTPException(403, "Ese registro de asistencia no es tuyo")

    fecha_actual = datetime.now(ZONA).date()
    if registro.fecha != fecha_actual:
        raise HTTPException(400, "Solo se puede corregir el registro de hoy")

    pendiente = db.query(models.SolicitudCorreccionAsistencia).filter(
        models.SolicitudCorreccionAsistencia.asistencia_id == registro.id,
        models.SolicitudCorreccionAsistencia.estado == "pendiente",
    ).first()
    if pendiente:
        raise HTTPException(400, "Ya tienes una solicitud pendiente")

    solicitud = models.SolicitudCorreccionAsistencia(
        asistencia_id=registro.id,
        usuario_id=registro.usuario_id,
        username=registro.username,
        fecha=registro.fecha,
        tipo=registro.tipo,
        motivo=body.motivo,
        estado="pendiente",
    )
    db.add(solicitud)
    db.commit()
    db.refresh(solicitud)
    return solicitud


@router.get("/solicitudes-correccion", response_model=List[schemas.SolicitudCorreccionResponse])
def listar_solicitudes_correccion(
    estado: Optional[str] = Query("pendiente"),
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in ("admin", "direccion"):
        raise HTTPException(403, "Solo admin y dirección")

    q = db.query(models.SolicitudCorreccionAsistencia)
    if estado:
        q = q.filter(models.SolicitudCorreccionAsistencia.estado == estado)
    return q.order_by(models.SolicitudCorreccionAsistencia.creado_en.desc()).all()


@router.post(
    "/solicitudes-correccion/{solicitud_id}/resolver",
    response_model=schemas.SolicitudCorreccionResponse,
)
def resolver_solicitud_correccion(
    solicitud_id: int,
    body: schemas.SolicitudCorreccionResolver,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in ("admin", "direccion"):
        raise HTTPException(403, "Solo admin y dirección")

    solicitud = db.query(models.SolicitudCorreccionAsistencia).filter(
        models.SolicitudCorreccionAsistencia.id == solicitud_id,
    ).first()
    if not solicitud:
        raise HTTPException(404, "Solicitud no encontrada")
    if solicitud.estado != "pendiente":
        raise HTTPException(400, f"La solicitud ya fue {solicitud.estado}")

    ahora = datetime.now(ZONA)

    try:
        solicitud.resuelto_por = current_user.id
        solicitud.resuelto_en = ahora
        solicitud.nota_admin = body.nota_admin

        if body.aprobar:
            registro = db.query(models.Asistencia).filter(
                models.Asistencia.id == solicitud.asistencia_id,
            ).first()

            solicitud.estado = "aprobada"
            # La FK es ON DELETE CASCADE: hay que soltar la referencia ANTES de
            # borrar la asistencia, si no la propia solicitud se borraría con ella
            # y se perdería el registro de quién la aprobó.
            solicitud.asistencia_id = None
            db.flush()

            if registro:
                # Las notificaciones ligadas a esta asistencia las borra Postgres
                # solo: esa FK tambien es ON DELETE CASCADE.
                db.delete(registro)
        else:
            solicitud.estado = "rechazada"

        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(solicitud)
    return solicitud


# ── Candado de check-in ───────────────────────────────────────────────────────

ROLES_CANDADO = ("asesor", "encargado")


def _cuentas_englobadas(db: Session, current_user):
    """Todas las cuentas de la misma persona (mismo nombre_englobado)."""
    eng = (current_user.nombre_englobado or "").strip()
    if not eng:
        return [current_user.id], [current_user.username or ""]

    filas = db.query(models.Usuario.id, models.Usuario.username).filter(
        func.upper(func.trim(models.Usuario.nombre_englobado)) == eng.upper()
    ).all()

    ids = [f[0] for f in filas] or [current_user.id]
    usernames = [f[1] or "" for f in filas] or [current_user.username or ""]
    return ids, usernames


def _estado_dia(db: Session, current_user, hoy: date) -> dict:
    """Calcula si la persona ya cumplió el requisito de asistencia de hoy."""
    if current_user.rol not in ROLES_CANDADO:
        return {
            "aplica_candado": False,
            "puede_usar": True,
            "hizo_checkin": True,
            "es_descanso": False,
            "englobado": current_user.nombre_englobado,
            "ruta_checkin": None,
            "fecha": hoy.isoformat(),
        }

    ids, usernames = _cuentas_englobadas(db, current_user)

    descanso = db.query(models.DiaDescanso).filter(
        models.DiaDescanso.usuario_id.in_(ids),
        models.DiaDescanso.fecha == hoy,
        models.DiaDescanso.cancelado_at.is_(None),
    ).first()

    # Fuente 1: módulos → tabla asistencia
    hizo_checkin = db.query(models.Asistencia).filter(
        models.Asistencia.usuario_id.in_(ids),
        models.Asistencia.fecha == hoy,
        models.Asistencia.tipo == "entrada",
    ).first() is not None

    # Fuente 2: Cadenas → tabla registros (idx = username)
    if not hizo_checkin and usernames:
        fila = db.execute(
            text(
                "SELECT 1 FROM registros "
                "WHERE TRIM(fecha) = :f "
                "AND UPPER(TRIM(idx)) = ANY(:us) "
                "AND entrada IS NOT NULL AND TRIM(entrada) <> '' LIMIT 1"
            ),
            {"f": hoy.isoformat(), "us": [u.strip().upper() for u in usernames if u]},
        ).first()
        hizo_checkin = fila is not None

    return {
        "aplica_candado": True,
        "puede_usar": bool(hizo_checkin or descanso),
        "hizo_checkin": hizo_checkin,
        "es_descanso": descanso is not None,
        "englobado": current_user.nombre_englobado,
        "ruta_checkin": "/asistencia",
        "fecha": hoy.isoformat(),
    }


@router.get("/estado-hoy")
def estado_hoy(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    return _estado_dia(db, current_user, datetime.now(ZONA).date())


@router.post("/dia-descanso")
def marcar_dia_descanso(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if current_user.rol not in ROLES_CANDADO:
        raise HTTPException(403, "Este registro es solo para asesores y encargados")

    hoy = datetime.now(ZONA).date()

    ya = db.query(models.DiaDescanso).filter(
        models.DiaDescanso.usuario_id == current_user.id,
        models.DiaDescanso.fecha == hoy,
    ).first()

    if ya is None:
        db.add(models.DiaDescanso(
            usuario_id=current_user.id,
            username=current_user.username,
            modulo_id=current_user.modulo_id,
            fecha=hoy,
        ))
        db.commit()
    elif ya.cancelado_at is not None:
        # UNIQUE (usuario_id, fecha) impide insertar de nuevo: se reactiva la fila
        ya.cancelado_at = None
        ya.cancelado_por = None
        db.commit()

    return _estado_dia(db, current_user, hoy)


@router.post("/dia-descanso/cancelar")
def cancelar_dia_descanso(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    """Marca como cancelado el descanso de hoy. No borra la fila: deja rastro."""
    if current_user.rol not in ROLES_CANDADO:
        raise HTTPException(403, "Este registro es solo para asesores y encargados")

    hoy = datetime.now(ZONA).date()
    ids, _ = _cuentas_englobadas(db, current_user)

    descanso = db.query(models.DiaDescanso).filter(
        models.DiaDescanso.usuario_id.in_(ids),
        models.DiaDescanso.fecha == hoy,
        models.DiaDescanso.cancelado_at.is_(None),
    ).first()

    if descanso is None:
        raise HTTPException(404, "No hay descanso activo hoy para cancelar")

    descanso.cancelado_at = datetime.now(ZONA)
    descanso.cancelado_por = current_user.id
    db.commit()

    return _estado_dia(db, current_user, hoy)
