"""Congelamiento de comisiones por falta de CHECK-OUT.

Fuente unica de verdad: si una persona registro entrada un dia pero nunca
registro salida, la comision de ese dia queda congelada (se calcula, no se
destruye). Solo admin/direccion puede descongelar, dejando nota.

Lee las DOS fuentes de asistencia, porque no todos escriben en las dos:
  - Modulos  -> tabla `asistencia` (tipo 'entrada' / 'salida')
  - Cadenas  -> tabla `registros`  (columnas TEXT `entrada` / `salida`)
Hay gente de Cadenas (C14, C32, C34, C37) que SOLO existe en `registros`.
Leer una sola fuente los congelaria todos los dias sin razon.

La API real es dias_congelados_batch: numero FIJO de consultas sin importar
cuantos empleados vengan. La nomina semanal procesa ~61 empleados y esto se
engancha dentro de bucles; una version por-empleado meteria un N+1 justo en el
unico punto que hoy resuelve todo con un agregado.
"""

import logging
from collections import defaultdict
from datetime import date

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app import models

logger = logging.getLogger(__name__)

# La regla no es retroactiva. Ningun dia anterior a esta fecha se congela jamas.
FECHA_INICIO_CONGELAMIENTO = date(2026, 9, 14)

# True  = el dia congelado se MARCA pero se paga igual (modo aviso).
# False = el dia congelado se descuenta de verdad.
# Mientras esta en True ningun monto cambia: solo se agrega el flag al resultado.
MODO_AVISO = False


def aplicar_congelamiento(monto, esta_congelado: bool) -> tuple:
    """Devuelve (monto_a_pagar, congelado_bool)."""
    if not esta_congelado:
        return monto, False
    if MODO_AVISO:
        return monto, True      # marca pero paga
    return 0, True              # congela de verdad


def _norm(valor) -> str:
    return (valor or "").strip().upper()


def _grupos_por_empleado(
    db: Session,
    empleado_ids: list[int],
) -> dict[int, tuple[list[int], list[str]]]:
    """{empleado_id: (ids, usernames)} de todas las cuentas del mismo englobado.

    Dos queries fijas a `usuarios`, sin importar cuantos empleados vengan.
    Empleado sin nombre_englobado -> su grupo es solo el mismo.
    Empleado que no existe en usuarios -> ([empleado_id], []).
    """
    filas_base = (
        db.query(
            models.Usuario.id,
            models.Usuario.username,
            models.Usuario.nombre_englobado,
        )
        .filter(models.Usuario.id.in_(empleado_ids))
        .all()
    )
    base_por_id = {f.id: f for f in filas_base}

    englobados = {_norm(f.nombre_englobado) for f in filas_base}
    englobados.discard("")

    # Todas las cuentas que comparten alguno de esos nombre_englobado.
    miembros_por_englobado: dict[str, list] = defaultdict(list)
    if englobados:
        filas_grupo = (
            db.query(
                models.Usuario.id,
                models.Usuario.username,
                models.Usuario.nombre_englobado,
            )
            .filter(
                func.upper(func.trim(models.Usuario.nombre_englobado)).in_(
                    sorted(englobados)
                )
            )
            .all()
        )
        for f in filas_grupo:
            miembros_por_englobado[_norm(f.nombre_englobado)].append(f)

    grupos: dict[int, tuple[list[int], list[str]]] = {}
    for eid in empleado_ids:
        base = base_por_id.get(eid)
        if base is None:
            grupos[eid] = ([eid], [])
            continue

        eng = _norm(base.nombre_englobado)
        miembros = miembros_por_englobado.get(eng) if eng else None
        if not miembros:
            grupos[eid] = ([base.id], [base.username or ""])
            continue

        grupos[eid] = (
            [m.id for m in miembros],
            [m.username or "" for m in miembros],
        )

    return grupos


def _marcas_asistencia(
    db: Session,
    usuario_ids: list[int],
    inicio: date,
    fin: date,
) -> tuple[dict[int, set[date]], dict[int, set[date]]]:
    """(entradas, salidas) por usuario_id, leidas de la tabla `asistencia`."""
    entradas: dict[int, set[date]] = defaultdict(set)
    salidas: dict[int, set[date]] = defaultdict(set)
    if not usuario_ids:
        return entradas, salidas

    filas = (
        db.query(
            models.Asistencia.usuario_id,
            models.Asistencia.fecha,
            models.Asistencia.tipo,
        )
        .filter(
            models.Asistencia.usuario_id.in_(usuario_ids),
            models.Asistencia.fecha.between(inicio, fin),
        )
        .all()
    )

    for f in filas:
        if f.tipo == "entrada":
            entradas[f.usuario_id].add(f.fecha)
        elif f.tipo == "salida":
            salidas[f.usuario_id].add(f.fecha)

    return entradas, salidas


def _marcas_cadenas(
    db: Session,
    usernames: list[str],
    inicio: date,
    fin: date,
) -> tuple[dict[str, set[date]], dict[str, set[date]]]:
    """(entradas, salidas) por username normalizado, de la tabla `registros`.

    `fecha`, `entrada` y `salida` son TEXT: se comparan trimmeados y la fecha se
    parsea con date.fromisoformat. Una fila que no parsea se ignora.
    """
    entradas: dict[str, set[date]] = defaultdict(set)
    salidas: dict[str, set[date]] = defaultdict(set)

    us = sorted({_norm(u) for u in usernames if _norm(u)})
    if not us:
        return entradas, salidas

    filas = db.execute(
        text(
            "SELECT UPPER(TRIM(idx)) AS u, "
            "       TRIM(fecha) AS f, "
            "       COALESCE(TRIM(entrada), '') AS ent, "
            "       COALESCE(TRIM(salida), '')  AS sal "
            "FROM registros "
            "WHERE UPPER(TRIM(idx)) = ANY(:us) "
            "  AND TRIM(fecha) BETWEEN :inicio AND :fin"
        ),
        {"us": us, "inicio": inicio.isoformat(), "fin": fin.isoformat()},
    ).all()

    for fila in filas:
        try:
            dia = date.fromisoformat((fila.f or "").strip())
        except (ValueError, TypeError):
            continue
        if fila.ent:
            entradas[fila.u].add(dia)
        if fila.sal:
            salidas[fila.u].add(dia)

    return entradas, salidas


def _descongelados(
    db: Session,
    usuario_ids: list[int],
    inicio: date,
    fin: date,
) -> dict[int, set[date]]:
    """Fechas descongeladas por admin/direccion, por usuario_id."""
    fuera: dict[int, set[date]] = defaultdict(set)
    if not usuario_ids:
        return fuera

    filas = (
        db.query(
            models.DescongelacionComision.usuario_id,
            models.DescongelacionComision.fecha,
        )
        .filter(
            models.DescongelacionComision.usuario_id.in_(usuario_ids),
            models.DescongelacionComision.cancelado_en.is_(None),
            models.DescongelacionComision.fecha.between(inicio, fin),
        )
        .all()
    )

    for f in filas:
        fuera[f.usuario_id].add(f.fecha)

    return fuera


def dias_congelados_batch(
    db: Session,
    empleado_ids: list[int],
    inicio: date,
    fin: date,
) -> dict[int, set[date]]:
    """{empleado_id: fechas con comision congelada} para todos de un jalon.

    Congelado = hubo entrada, no hubo salida, y no fue descongelado por admin.
    El numero de consultas es fijo (no crece con len(empleado_ids)); todo el
    cruce por grupo englobado se resuelve en memoria.

    Falla abierto: ante cualquier error devuelve set() vacio para todos.
    Preferimos pagar de mas que congelar sin razon.
    """
    ids_unicos = list(dict.fromkeys(empleado_ids))
    if not ids_unicos:
        return {}

    vacio = {eid: set() for eid in ids_unicos}

    try:
        inicio_efectivo = max(inicio, FECHA_INICIO_CONGELAMIENTO)
        if inicio_efectivo > fin:
            return vacio

        grupos = _grupos_por_empleado(db, ids_unicos)

        todos_ids = sorted({uid for ids, _ in grupos.values() for uid in ids})
        todos_usernames = sorted(
            {_norm(u) for _, usrs in grupos.values() for u in usrs if _norm(u)}
        )

        ent_uid, sal_uid = _marcas_asistencia(db, todos_ids, inicio_efectivo, fin)
        ent_user, sal_user = _marcas_cadenas(db, todos_usernames, inicio_efectivo, fin)
        desc_uid = _descongelados(db, todos_ids, inicio_efectivo, fin)

        resultado: dict[int, set[date]] = {}
        for eid in ids_unicos:
            ids, usernames = grupos[eid]

            entradas: set[date] = set()
            salidas: set[date] = set()
            fuera: set[date] = set()

            for uid in ids:
                entradas |= ent_uid.get(uid, set())
                salidas |= sal_uid.get(uid, set())
                fuera |= desc_uid.get(uid, set())

            for u in usernames:
                clave = _norm(u)
                if not clave:
                    continue
                entradas |= ent_user.get(clave, set())
                salidas |= sal_user.get(clave, set())

            resultado[eid] = entradas - salidas - fuera

        return resultado

    except Exception:
        logger.exception(
            "dias_congelados_batch fallo para %s empleados rango=%s..%s; "
            "no se congela nada",
            len(ids_unicos), inicio, fin,
        )
        return vacio


def dias_congelados(
    db: Session,
    empleado_id: int,
    inicio: date,
    fin: date,
) -> set[date]:
    """Atajo de un solo empleado. Unica implementacion: la batch."""
    return dias_congelados_batch(db, [empleado_id], inicio, fin).get(empleado_id, set())
