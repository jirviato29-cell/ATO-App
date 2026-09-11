import base64
import logging
import os
import traceback
from datetime import datetime, date as _date, time as _time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models, schemas
from app.config import get_current_user
from app.database import get_db
from app.lector_capturas import _a_24h, leer_captura

router = APIRouter(prefix="/capturas-telcel", tags=["capturas-telcel"])

log = logging.getLogger(__name__)

ZONA = ZoneInfo("America/Mexico_City")
ROLES_ADMIN = ("admin", "direccion")
MODULO_CADENAS = 7
TOPE_JORNADA_MIN = 16 * 60


def _err(codigo: str, mensaje: str, status: int = 400):
    raise HTTPException(status, detail={"codigo": codigo, "mensaje": mensaje})


def _supabase_admin():
    from supabase import create_client
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise HTTPException(500, "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY no configurados")
    return create_client(url, key)


def _rol(user) -> str:
    return user.rol.value if hasattr(user.rol, "value") else str(user.rol)


def _decodificar_foto(foto_base64: str) -> bytes:
    raw = (foto_base64 or "").strip()
    if "," in raw:
        raw = raw.split(",", 1)[1].strip()
    if not raw:
        _err("FOTO_INVALIDA", "La imagen llegó vacía. Vuelve a subirla.")
    try:
        img = base64.b64decode(raw)
    except Exception:
        _err("FOTO_INVALIDA", "La imagen está corrupta. Vuelve a subirla.")
    if len(img) < 3072:
        _err("FOTO_INVALIDA", "La imagen es demasiado pequeña. Vuelve a subirla.")
    return img


def _candidatas_salida(datos):
    """A partir de entrada_hora/salida_hora crudos de json_ia,
    arma cada hora en su lectura original y en la lectura con el
    meridiano contrario (+/- 12h). Devuelve lista de time sin
    duplicados."""
    formato = datos.get("formato_hora")
    # En pantallas de 24h no hay meridiano que malinterpretar.
    es_24h = (formato or "").strip().lower().replace(" ", "") in ("24h", "24")
    contrario = {"am": "pm", "pm": "am"}

    candidatas = []
    for campo in ("entrada", "salida"):
        hora = datos.get(f"{campo}_hora")
        meridiano = datos.get(f"{campo}_meridiano")
        lecturas = [meridiano]
        if not es_24h and meridiano:
            mer = str(meridiano).strip().lower().replace(".", "").replace(" ", "")
            if mer in contrario:
                lecturas.append(contrario[mer])
        for mer in lecturas:
            valor = _a_24h(hora, mer, formato)
            if not valor:
                continue
            t = _time.fromisoformat(valor)
            if t not in candidatas:
                candidatas.append(t)
    return candidatas


def _reparar_salida_por_duracion(t_entrada, candidatas, dur_texto,
                                 tolerancia=5):
    """Devuelve la unica hora de salida cuya duracion contra
    t_entrada cae dentro de +/- tolerancia minutos de dur_texto.
    Devuelve None si hay cero coincidencias o mas de una."""
    if t_entrada is None or dur_texto is None:
        return None
    min_entrada = t_entrada.hour * 60 + t_entrada.minute
    coincidencias = []
    for t in candidatas:
        dur = (t.hour * 60 + t.minute) - min_entrada
        if dur <= 0 or dur > TOPE_JORNADA_MIN:
            continue
        if abs(dur - dur_texto) <= tolerancia and t not in coincidencias:
            coincidencias.append(t)
    return coincidencias[0] if len(coincidencias) == 1 else None


def _espejo_registros(db, username: str, fecha, tipo: str,
                      hora_entrada, hora_salida, duracion_minutos,
                      clave=None):
    """Refleja la captura BES en la tabla registros, que es de donde
    sale el bono. Formato identico al que escribe ZEliCheck."""
    fecha_str = fecha.isoformat()
    idx = username

    if tipo == "apertura":
        # Protegido: solo llena una fila vacia. Si ya hay entrada o salida
        # (ZEliCheck o correccion manual), no toca nada.
        db.execute(text("""
            INSERT INTO registros (fecha, idx, entrada, salida, horas, cumple)
            VALUES (:fecha, :idx, :entrada, NULL, NULL, NULL)
            ON CONFLICT (fecha, idx) DO UPDATE SET
                entrada = EXCLUDED.entrada
            WHERE registros.salida IS NULL
              AND registros.entrada IS NULL
        """), {"fecha": fecha_str, "idx": idx, "entrada": hora_entrada})
        return

    # cierre: fila completa
    # Si la IA no pudo leer la duracion, se reconstruye de las horas. Sin
    # correccion por medianoche: una resta negativa o desproporcionada
    # significa lectura mala, y en ese caso no se escribe la fila espejo.
    if duracion_minutos is None and hora_entrada and hora_salida:
        he, me = map(int, hora_entrada.split(":"))
        hs, ms = map(int, hora_salida.split(":"))
        calculada = (hs * 60 + ms) - (he * 60 + me)
        if 0 < calculada <= 960:
            duracion_minutos = calculada
        else:
            log.warning(
                "espejo_registros: duracion fuera de rango (%s min), no se "
                "escribe fila. username=%s fecha=%s clave=%s entrada=%s salida=%s",
                calculada, username, fecha_str, clave, hora_entrada, hora_salida,
            )
            return

    if duracion_minutos is None:
        log.warning(
            "espejo_registros: sin duracion, no se escribe fila. "
            "username=%s fecha=%s clave=%s", username, fecha_str, clave,
        )
        return

    horas = round(duracion_minutos / 60, 2)
    cumple_str = "TRUE" if duracion_minutos >= 360 else "FALSE"

    db.execute(text("""
        INSERT INTO registros (fecha, idx, entrada, salida, horas, cumple)
        VALUES (:fecha, :idx, :entrada, :salida, :horas, :cumple)
        ON CONFLICT (fecha, idx) DO UPDATE SET
            entrada = EXCLUDED.entrada,
            salida  = EXCLUDED.salida,
            horas   = EXCLUDED.horas,
            cumple  = EXCLUDED.cumple
        WHERE registros.cumple IS DISTINCT FROM 'TRUE'
    """), {
        "fecha": fecha_str, "idx": idx,
        "entrada": hora_entrada, "salida": hora_salida,
        "horas": horas, "cumple": cumple_str,
    })


@router.post("/subir", response_model=schemas.CapturaTelcelResponse)
def subir_captura(
    body: schemas.CapturaTelcelCreate,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    rol = _rol(current_user)
    es_admin = rol in ROLES_ADMIN

    if body.tipo not in ("apertura", "cierre"):
        _err("TIPO_INVALIDO", "Tipo inválido.")

    # A quien se le registra
    if body.usuario_id and body.usuario_id != current_user.id:
        if not es_admin:
            _err("SIN_PERMISO", "No puedes subir capturas de otra persona.", 403)
        destino = db.query(models.Usuario).filter(
            models.Usuario.id == body.usuario_id
        ).first()
        if not destino:
            _err("USUARIO_NO_ENCONTRADO", "Usuario no encontrado.", 404)
    else:
        destino = current_user
        if rol not in ("asesor", "encargado") and not es_admin:
            _err("SIN_PERMISO", "Tu rol no registra capturas.", 403)

    if destino.modulo_id != MODULO_CADENAS:
        _err("MODULO_NO_APLICA",
             "Las capturas de Telcel solo aplican para promotores de Cadenas.", 403)

    if not destino.tienda_id:
        _err("SIN_TIENDA", f"{destino.username} no tiene tienda asignada. Reporta a administración.")

    # Corte barato: si ya existe la de hoy, no gastamos llamada a la IA
    hoy_pre = datetime.now(ZONA).date()
    ya_existe = db.query(models.CapturaTelcel).filter(
        models.CapturaTelcel.usuario_id == destino.id,
        models.CapturaTelcel.fecha == hoy_pre,
        models.CapturaTelcel.tipo == body.tipo,
    ).first()
    if ya_existe:
        _err("DUPLICADA",
             f"Ya está registrada la captura de {body.tipo} de {destino.username} "
             f"para hoy.")

    # Leer la imagen
    img_bytes = _decodificar_foto(body.foto_base64)

    try:
        datos = leer_captura(img_bytes)
    except Exception:
        traceback.print_exc()
        _err("LECTOR_NO_DISPONIBLE",
             "No se pudo procesar la imagen en este momento. Intenta de nuevo en un minuto.", 503)

    if not datos.get("legible"):
        _err("ILEGIBLE",
             "No se pudo leer la captura. Asegúrate de que sea la pantalla completa "
             "de la app de Telcel, sin recortar y sin reflejos.")

    # Fecha
    try:
        fecha_captura = _date.fromisoformat(datos["fecha"])
    except (KeyError, TypeError, ValueError):
        _err("FECHA_ILEGIBLE", "No se pudo leer la fecha de la captura. Vuelve a tomarla.")

    hoy = datetime.now(ZONA).date()
    if fecha_captura != hoy and not es_admin:
        _err("FECHA_NO_ES_HOY",
             f"Esa captura es del {fecha_captura.strftime('%d/%m/%Y')} y hoy es "
             f"{hoy.strftime('%d/%m/%Y')}. Solo se acepta la del día. "
             "Si se te pasó, pídele a administración que la suba.")
    if fecha_captura > hoy:
        _err("FECHA_FUTURA", "La captura tiene fecha futura.")

    # Clave: solo informativa. Ya no valida ni bloquea.
    # El texto del encabezado cambia segun el telefono y generaba
    # falsos rechazos. Se guarda lo que se alcance a leer.
    clave_leida = (datos.get("clave") or "").strip().upper()
    if not clave_leida:
        _err("SIN_CLAVE",
             "No se alcanza a leer el codigo del encabezado de la app de Telcel. "
             "Sube la pantalla completa de la app, no una captura de otra pantalla.")

    # Horas
    if not datos.get("hora_entrada"):
        _err("SIN_ENTRADA", "La captura no muestra la hora de entrada.")

    if body.tipo == "cierre" and not datos.get("hora_salida"):
        _err("SIN_SALIDA",
             "Esa captura todavía no tiene el check-out. Sube la de cierre de jornada.")

    if body.tipo == "apertura" and datos.get("hora_salida"):
        _err("ES_CIERRE",
             "Esa captura ya trae el check-out. Súbela como cierre de jornada.")

    # Sin apertura no hay cierre
    if body.tipo == "cierre":
        apertura = db.query(models.CapturaTelcel).filter(
            models.CapturaTelcel.usuario_id == destino.id,
            models.CapturaTelcel.fecha == fecha_captura,
            models.CapturaTelcel.tipo == "apertura",
        ).first()
        if not apertura:
            _err("SIN_APERTURA",
                 "No tienes registrada la captura de apertura de hoy. "
                 "Sube primero la de la mañana.")

    # Duplicado
    duplicado = db.query(models.CapturaTelcel).filter(
        models.CapturaTelcel.usuario_id == destino.id,
        models.CapturaTelcel.fecha == fecha_captura,
        models.CapturaTelcel.tipo == body.tipo,
    ).first()
    if duplicado:
        _err("DUPLICADA",
             f"Ya está registrada la captura de {body.tipo} de {destino.username} "
             f"para el {fecha_captura.strftime('%d/%m/%Y')}.")

    # Subir al bucket
    ts = int(datetime.now(ZONA).timestamp())
    filename = f"telcel_{destino.username}_{fecha_captura.isoformat()}_{body.tipo}_{ts}.jpg"

    try:
        supabase = _supabase_admin()
        supabase.storage.from_("asistencia-fotos").upload(
            path=filename,
            file=img_bytes,
            file_options={"content-type": "image/jpeg", "upsert": "true"},
        )
    except HTTPException:
        raise
    except Exception:
        traceback.print_exc()
        _err("STORAGE_ERROR",
             "No se pudo guardar la imagen. Intenta de nuevo.", 503)

    foto_url = (
        f"{os.getenv('SUPABASE_URL')}/storage/v1/object/public/"
        f"asistencia-fotos/{filename}"
    )

    def _a_time(valor):
        if not valor:
            return None
        try:
            return _time.fromisoformat(valor)
        except (ValueError, TypeError):
            return None

    t_entrada = _a_time(datos.get("hora_entrada"))
    t_salida = _a_time(datos.get("hora_salida"))

    if t_entrada is None:
        _err("HORA_INVALIDA", "No se pudo interpretar la hora de la captura. Vuelve a tomarla.")

    if body.tipo == "cierre":
        # 1. Si la IA entrego la salida antes que la entrada, invirtio los
        #    recuadros. Corregir ANTES de tocar nada mas: si se pisa primero
        #    la entrada con la de la apertura, la salida real se pierde.
        if t_salida is not None and t_entrada is not None and t_salida < t_entrada:
            log.warning(
                "captura cierre con recuadros invertidos por la IA, se corrige "
                "por swap. username=%s fecha=%s clave=%s ia_entrada=%s ia_salida=%s",
                destino.username, fecha_captura.isoformat(), clave_leida,
                t_entrada.strftime("%H:%M"), t_salida.strftime("%H:%M"),
            )
            t_entrada, t_salida = t_salida, t_entrada

        # 2. Ya con el orden corregido, la entrada la manda SIEMPRE la
        #    apertura ya guardada. La IA no vuelve a opinar sobre la entrada.
        if apertura.hora_entrada:
            t_entrada = apertura.hora_entrada

        # 3. Calcular duracion ya con las horas resueltas. Sin +=1440.
        dur_calc = None
        if t_entrada is not None and t_salida is not None:
            dur_calc = (
                (t_salida.hour * 60 + t_salida.minute)
                - (t_entrada.hour * 60 + t_entrada.minute)
            )

        dur_texto = datos.get("duracion_texto_minutos")

        # 3b. Antes de rechazar, intentar reparar un meridiano mal leido: con
        #     la entrada ya fijada por la apertura, buscar la unica lectura de
        #     salida (original o con meridiano contrario) que cuadre con
        #     duracion_texto. Si no hay exactamente una, se rechaza abajo.
        if (apertura.hora_entrada and dur_texto is not None
                and dur_calc is not None and abs(dur_texto - dur_calc) > 5):
            reparada = _reparar_salida_por_duracion(
                t_entrada, _candidatas_salida(datos), dur_texto
            )
            if reparada is not None:
                salida_original = t_salida
                t_salida = reparada
                dur_calc = (
                    (t_salida.hour * 60 + t_salida.minute)
                    - (t_entrada.hour * 60 + t_entrada.minute)
                )
                datos["_meridiano_corregido"] = True
                datos["_salida_original"] = salida_original.strftime("%H:%M")
                log.warning(
                    "captura cierre con meridiano reparado por duracion_texto. "
                    "usuario_id=%s username=%s fecha=%s clave=%s entrada=%s "
                    "salida_original=%s salida_reparada=%s dur_texto=%s dur_calc=%s",
                    destino.id, destino.username, fecha_captura.isoformat(),
                    clave_leida, t_entrada.strftime("%H:%M"),
                    datos["_salida_original"], t_salida.strftime("%H:%M"),
                    dur_texto, dur_calc,
                )

        # 4. duracion_texto es el testigo. Si existe y discrepa mas de 5 min
        #    del calculo, no confiamos en la lectura: rechazar.
        if dur_texto is not None and dur_calc is not None:
            if abs(dur_texto - dur_calc) > 5:
                _err(
                    "LECTURA_INCONSISTENTE",
                    "Las horas leidas no coinciden con la duracion que muestra "
                    "la pantalla. Vuelve a tomar la captura completa."
                )

        # 5. Duracion final: manda el texto de la pantalla; la resta es respaldo.
        duracion_final = dur_texto if dur_texto is not None else dur_calc

        # 6. Cordura: negativa o mayor al tope = lectura mala, no se guarda.
        if (duracion_final is None or duracion_final <= 0
                or duracion_final > TOPE_JORNADA_MIN):
            _err(
                "DURACION_INVALIDA",
                "No se pudo determinar una jornada valida en la captura. "
                "Vuelve a tomarla."
            )

        datos["duracion_minutos"] = duracion_final

    registro = models.CapturaTelcel(
        usuario_id=destino.id,
        username=destino.username,
        modulo_id=destino.modulo_id,
        tienda_id=destino.tienda_id,
        fecha=fecha_captura,
        tipo=body.tipo,
        clave=clave_leida,
        hora_entrada=t_entrada,
        hora_salida=t_salida,
        duracion_minutos=datos.get("duracion_minutos"),
        foto_url=foto_url,
        json_ia=datos,
    )
    db.add(registro)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        _err("DUPLICADA",
             f"Ya está registrada la captura de {body.tipo} de {destino.username} "
             f"para el {fecha_captura.strftime('%d/%m/%Y')}.")

    # Espejo a registros (bono). Si falla, la captura ya quedo guardada.
    try:
        _espejo_registros(
            db,
            username=destino.username,
            fecha=fecha_captura,
            tipo=body.tipo,
            hora_entrada=t_entrada.strftime("%H:%M") if t_entrada else None,
            hora_salida=t_salida.strftime("%H:%M") if t_salida else None,
            duracion_minutos=datos.get("duracion_minutos"),
            clave=clave_leida,
        )
        db.commit()
    except Exception:
        db.rollback()
        traceback.print_exc()

    db.refresh(registro)
    return registro


@router.get("/aplica")
def aplica_capturas(
    current_user: models.Usuario = Depends(get_current_user),
):
    return {
        "aplica": current_user.modulo_id == MODULO_CADENAS
        and _rol(current_user) in ("asesor", "encargado"),
    }


@router.get("/mis-capturas")
def mis_capturas(
    fecha: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if fecha:
        try:
            dia = _date.fromisoformat(fecha)
        except ValueError:
            _err("FECHA_INVALIDA", "Fecha invalida.")
    else:
        dia = datetime.now(ZONA).date()

    filas = db.query(models.CapturaTelcel).filter(
        models.CapturaTelcel.usuario_id == current_user.id,
        models.CapturaTelcel.fecha == dia,
    ).all()

    out = {"fecha": dia.isoformat(), "apertura": None, "cierre": None}
    for f in filas:
        out[f.tipo] = {
            "id": f.id,
            "clave": f.clave,
            "hora_entrada": f.hora_entrada.strftime("%H:%M") if f.hora_entrada else None,
            "hora_salida": f.hora_salida.strftime("%H:%M") if f.hora_salida else None,
            "duracion_minutos": f.duracion_minutos,
            "foto_url": f.foto_url,
            "subido_at": f.subido_at.isoformat() if f.subido_at else None,
        }
    return out


@router.get("/resumen-dia")
def resumen_dia(
    fecha: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    if _rol(current_user) not in ROLES_ADMIN:
        _err("SIN_PERMISO", "No tienes permiso para ver esto.", 403)

    if fecha:
        try:
            dia = _date.fromisoformat(fecha)
        except ValueError:
            _err("FECHA_INVALIDA", "Fecha invalida.")
    else:
        dia = datetime.now(ZONA).date()

    promotores = db.query(models.Usuario).filter(
        models.Usuario.modulo_id == MODULO_CADENAS,
        models.Usuario.activo == True,
    ).order_by(models.Usuario.username).all()

    filas = db.query(models.CapturaTelcel).filter(
        models.CapturaTelcel.fecha == dia,
    ).all()

    por_usuario = {}
    for f in filas:
        por_usuario.setdefault(f.usuario_id, {})[f.tipo] = f

    def _pack(f):
        if not f:
            return None
        return {
            "id": f.id,
            "clave": f.clave,
            "hora_entrada": f.hora_entrada.strftime("%H:%M") if f.hora_entrada else None,
            "hora_salida": f.hora_salida.strftime("%H:%M") if f.hora_salida else None,
            "duracion_minutos": f.duracion_minutos,
            "foto_url": f.foto_url,
        }

    resultado = []
    for p in promotores:
        d = por_usuario.get(p.id, {})
        resultado.append({
            "usuario_id": p.id,
            "username": p.username,
            "nombre_completo": p.nombre_completo,
            "apertura": _pack(d.get("apertura")),
            "cierre": _pack(d.get("cierre")),
        })

    completos = sum(1 for r in resultado if r["apertura"] and r["cierre"])
    parciales = sum(1 for r in resultado if bool(r["apertura"]) != bool(r["cierre"]))
    sin_nada = sum(1 for r in resultado if not r["apertura"] and not r["cierre"])

    return {
        "fecha": dia.isoformat(),
        "total": len(resultado),
        "completos": completos,
        "parciales": parciales,
        "sin_nada": sin_nada,
        "promotores": resultado,
    }


def _dias_semana_bes():
    d = datetime.now(ZONA)
    dom = d - timedelta(days=d.weekday() + 1) if d.weekday() != 6 else d
    return [(dom + timedelta(days=i)).date() for i in range(7)]


@router.get("/mi-semana")
def mi_semana(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    dias = _dias_semana_bes()
    hoy = datetime.now(ZONA).date()

    # La semana sale de registros: incluye lo capturado a mano en
    # ZEliCheck y lo espejado desde BES. El ciclo es hibrido.
    filas = db.execute(text("""
        SELECT TRIM(fecha) AS fecha, entrada, salida, horas, cumple
        FROM registros
        WHERE UPPER(TRIM(idx)) = UPPER(TRIM(:idx))
          AND TRIM(fecha) BETWEEN :desde AND :hasta
    """), {
        "idx": current_user.username,
        "desde": dias[0].isoformat(),
        "hasta": dias[6].isoformat(),
    }).mappings().all()

    por_dia = {f["fecha"]: f for f in filas}

    # Que dias tienen captura BES, para distinguir el origen
    caps = db.query(models.CapturaTelcel).filter(
        models.CapturaTelcel.usuario_id == current_user.id,
        models.CapturaTelcel.fecha >= dias[0],
        models.CapturaTelcel.fecha <= dias[6],
    ).all()
    tipos_por_dia = {}
    for c in caps:
        tipos_por_dia.setdefault(c.fecha.isoformat(), set()).add(c.tipo)

    resultado = []
    for d in dias:
        k = d.isoformat()
        r = por_dia.get(k)
        t = tipos_por_dia.get(k, set())
        cumple_txt = (r["cumple"] or "").strip().upper() if r else ""
        resultado.append({
            "fecha": k,
            "apertura": "apertura" in t,
            "cierre": "cierre" in t,
            "hora_entrada": (r["entrada"] or None) if r else None,
            "hora_salida": (r["salida"] or None) if r else None,
            "duracion_minutos": (
                int(round(float(r["horas"]) * 60))
                if r and r["horas"] is not None else None
            ),
            "cumple": True if cumple_txt == "TRUE" else (
                False if cumple_txt == "FALSE" else None
            ),
        })

    return {"dias": resultado, "hoy": hoy.isoformat()}
