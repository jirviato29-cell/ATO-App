import re
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.config import get_current_user
from app.congelamiento import aplicar_congelamiento, dias_congelados_batch
from app.database import get_db

router = APIRouter()


def _get_nomina_publicada(db: Session) -> models.Nomina:
    nomina = db.query(models.Nomina).filter(models.Nomina.publicada == True).first()
    if not nomina:
        raise HTTPException(404, "No hay nómina publicada actualmente")
    return nomina


def _get_mi_fila(nomina: models.Nomina, current_user: models.Usuario) -> dict:
    fila = next(
        (item for item in nomina.datos if current_user.id in item.get("usuario_ids", [])),
        None,
    )
    if not fila:
        raise HTTPException(404, "No apareces en la nómina publicada")
    return fila


def _build_periodos(nomina: models.Nomina) -> dict:
    periodos: dict = {}
    if nomina.ciclo_horas_extras:
        periodos["horas_extras"] = {
            "inicio": str(nomina.ciclo_horas_extras.fecha_inicio),
            "fin": str(nomina.ciclo_horas_extras.fecha_fin),
        }
    if nomina.fecha_inicio_asesores:
        periodos["asesores"] = {
            "inicio": str(nomina.fecha_inicio_asesores),
            "fin": str(nomina.fecha_fin_asesores),
        }
    if nomina.fecha_inicio_encargados:
        periodos["encargados"] = {
            "inicio": str(nomina.fecha_inicio_encargados),
            "fin": str(nomina.fecha_fin_encargados),
        }
    if nomina.fecha_inicio_cadenas:
        periodos["cadenas"] = {
            "inicio": str(nomina.fecha_inicio_cadenas),
            "fin": str(nomina.fecha_fin_cadenas),
        }
    return periodos


@router.get("/mi-recibo")
def mi_recibo(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    nomina = _get_nomina_publicada(db)
    fila = _get_mi_fila(nomina, current_user)
    # El desglose de sueldo por modulo (sueldo_detalle, sueldo_suma_modulos,
    # sueldo_minimo_aplicado) ya viene dentro de la fila, escrito por
    # CrearNominaDialog al guardar la nomina. No consultar ciclos_guardados:
    # nomina.ciclo_sueldos_encargados_id nunca se llena.
    return {
        "etiqueta": nomina.etiqueta,
        "creado_en": nomina.creado_en.isoformat() if nomina.creado_en else None,
        "periodos": _build_periodos(nomina),
        "fila": fila,
    }


@router.get("/mi-recibo/pdf")
def mi_recibo_pdf(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    nomina = _get_nomina_publicada(db)
    fila = _get_mi_fila(nomina, current_user)
    periodos = _build_periodos(nomina)

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import (
        HRFlowable, SimpleDocTemplate, Spacer, Table, TableStyle, Paragraph,
    )

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=50, rightMargin=50, topMargin=50, bottomMargin=50,
    )
    styles = getSampleStyleSheet()
    ato_orange = colors.HexColor("#FF6600")

    h1 = ParagraphStyle("h1", parent=styles["Heading1"], textColor=ato_orange, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=ato_orange, fontSize=11, spaceAfter=4)

    def fmt(v): return f"${float(v or 0):.2f}"

    he = fila.get("horas_extra")
    he_str = "—" if he is None else (f"+{float(he):.1f} hrs" if float(he) > 0 else f"{float(he):.1f} hrs")

    elements = []
    elements.append(Paragraph("ATO Sistema — Recibo de Nómina", h1))
    creado_str = nomina.creado_en.strftime("%d/%m/%Y %H:%M") if nomina.creado_en else ""
    elements.append(Paragraph(f"Nómina: <b>{nomina.etiqueta}</b>  |  Emitida: {creado_str}", styles["Normal"]))
    elements.append(Spacer(1, 10))

    # Employee info
    elements.append(Paragraph("EMPLEADO", h2))
    seccion_map = {"asesor": "Asesor", "encargado": "Encargado", "cadena": "Cadena Comercial"}
    seccion_label = seccion_map.get(fila.get("seccion", ""), fila.get("seccion", ""))
    emp_table = Table(
        [["Nombre:", fila.get("empleado", "")], ["Sección:", seccion_label]],
        colWidths=[110, 340],
    )
    emp_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    elements.append(emp_table)
    elements.append(Spacer(1, 10))

    # Periodos
    if periodos:
        elements.append(Paragraph("PERIODOS DE PAGO", h2))
        periodo_labels = {
            "horas_extras": "Horas Extra:",
            "asesores": "Comisiones:",
            "encargados": "Com. Encargados:",
            "cadenas": "Com. Cadenas:",
        }
        period_rows = [
            [label, f"{p['inicio']}  →  {p['fin']}"]
            for key, label in periodo_labels.items()
            if (p := periodos.get(key))
        ]
        if period_rows:
            p_table = Table(period_rows, colWidths=[160, 290])
            p_table.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]))
            elements.append(p_table)
        elements.append(Spacer(1, 10))

    # Desglose
    elements.append(Paragraph("DESGLOSE", h2))
    desglose_rows = [
        ["Sueldo base:", fmt(fila.get("sueldo", 0))],
        ["Horas extra:", he_str],
        ["Pago H. Extra:", fmt(fila.get("pago_he", 0))],
        ["Accesorios:", fmt(fila.get("accesorios", 0))],
        ["Teléfonos:", fmt(fila.get("telefonos", 0))],
        ["Chips:", fmt(fila.get("chips", 0))],
        ["Incubadora:", fmt(fila.get("incubadora", 0))],
    ]
    # Líneas de incubadora pagadas, debajo de su renglón (solo cadena).
    inc_ini = len(desglose_rows)
    for ch in _incubadora_lineas(db, nomina, fila):
        desglose_rows.append([
            f"      {_fecha_dd_mes(ch['fecha'])}   {ch['numero_telefono']}",
            fmt(ch["comision"]),
        ])
    inc_fin = len(desglose_rows) - 1
    desglose_rows += [
        ["Planes tarifarios:", fmt(fila.get("planes", 0))],
        ["Com. pendientes:", fmt(fila.get("pendientes", 0))],
        ["Bonos:", fmt(fila.get("bonos", 0))],
        ["Subtotal:", fmt(fila.get("subtotal", 0))],
        ["Sanciones:", f"-{fmt(fila.get('sanciones', 0))}"],
    ]
    i_sub = len(desglose_rows) - 2
    i_san = len(desglose_rows) - 1
    estilo = [
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, i_sub), (-1, i_san), "Helvetica-Bold"),
        ("TEXTCOLOR", (1, i_san), (1, i_san), colors.HexColor("#dc2626")),
        ("LINEABOVE", (0, i_sub), (-1, i_sub), 0.5, colors.HexColor("#e2e8f0")),
    ]
    if inc_fin >= inc_ini:
        estilo += [
            ("FONTSIZE", (0, inc_ini), (-1, inc_fin), 8.5),
            ("TEXTCOLOR", (0, inc_ini), (-1, inc_fin), colors.HexColor("#475569")),
            ("TOPPADDING", (0, inc_ini), (-1, inc_fin), 1),
            ("BOTTOMPADDING", (0, inc_ini), (-1, inc_fin), 1),
        ]
    d_table = Table(desglose_rows, colWidths=[200, 250])
    d_table.setStyle(TableStyle(estilo))
    elements.append(d_table)

    elements.append(Spacer(1, 6))
    elements.append(HRFlowable(width="100%", thickness=1.5, color=ato_orange))
    elements.append(Spacer(1, 6))

    total_table = Table(
        [["DEPÓSITO TOTAL:", fmt(fila.get("deposito", 0))]],
        colWidths=[200, 250],
    )
    total_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 14),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("TEXTCOLOR", (1, 0), (1, 0), colors.HexColor("#16a34a")),
    ]))
    elements.append(total_table)

    doc.build(elements)
    buf.seek(0)

    emp_name = re.sub(r"[^\w\-]", "_", fila.get("empleado", "recibo"))[:40]
    fname = f"recibo_{emp_name}.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ── Detalle del recibo (solo lectura, no afecta el cálculo de nómina) ─────────

_EXTRA_POR_TIPO = {"Contado": 10, "Paguitos": 110, "Pajoy": 100}


def _comision_unitaria(v) -> float:
    """Misma fórmula que services.calcular_totales_comisiones."""
    base = float(getattr(getattr(v, "comision_obj", None), "cantidad", 0) or 0)
    if v.tipo_producto == "telefono":
        tipo = v.tipo_venta or ""
        if tipo == "Contado" and base > 0:
            return base
        return base + _EXTRA_POR_TIPO.get(tipo, 0)
    return base


_MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def _fecha_dd_mes(iso: str) -> str:
    """'2026-09-16' -> '16-sep'."""
    try:
        _, m, d = iso.split("-")
        return f"{d}-{_MESES[int(m) - 1]}"
    except (ValueError, IndexError, AttributeError):
        return str(iso)


def _incubadora_lineas(db: Session, nomina, fila) -> list:
    """Chips de incubadora que se pagaron en esta nómina, solo sección cadena.

    Nóminas nuevas: los guarda crear/recalcular en fila["incubadora_chips"].
    Nóminas viejas: se reconstruyen por fecha_validacion desde la nómina
    anterior, primero hasta el creado_en de esta y, si no cuadra, hasta el de
    la siguiente (una nómina recalculada paga chips validados después de
    crearse). Si ninguna ventana suma exactamente fila["incubadora"] no se
    mandan líneas (el recibo sigue mostrando solo el total)."""
    if (fila.get("seccion") or "").strip() != "cadena":
        return []

    guardados = fila.get("incubadora_chips")
    if guardados:
        return guardados

    total = round(float(fila.get("incubadora", 0) or 0), 2)
    ids = fila.get("usuario_ids", [])
    if total <= 0 or not ids or not nomina.creado_en:
        return []

    anterior = (
        db.query(models.Nomina.creado_en)
        .filter(models.Nomina.creado_en < nomina.creado_en)
        .order_by(models.Nomina.creado_en.desc())
        .first()
    )
    siguiente = (
        db.query(models.Nomina.creado_en)
        .filter(models.Nomina.creado_en > nomina.creado_en)
        .order_by(models.Nomina.creado_en.asc())
        .first()
    )

    def _chips_hasta(limite):
        q = db.query(models.VentaChip).filter(
            models.VentaChip.empleado_id.in_(ids),
            models.VentaChip.es_incubadora == True,
            models.VentaChip.validado == True,
            models.VentaChip.comision_pagada == True,
            models.VentaChip.cancelada == False,
            models.VentaChip.fecha_validacion <= limite,
        )
        if anterior:
            q = q.filter(models.VentaChip.fecha_validacion > anterior[0])
        return q.all()

    # total viene como float de JSON (ej. 88.78999999999999): comparar a centavos.
    def _cuadra(chips):
        return abs(sum(float(c.comision or 0) for c in chips) - total) < 0.005

    chips = _chips_hasta(nomina.creado_en)
    if not _cuadra(chips):
        chips = _chips_hasta(siguiente[0] if siguiente else func.now())
        if not _cuadra(chips):
            return []

    return [
        {
            "id": c.id,
            "fecha": str(c.fecha),
            "numero_telefono": c.numero_telefono,
            "tipo_chip": c.tipo_chip,
            "comision": round(float(c.comision or 0), 2),
        }
        for c in sorted(chips, key=lambda c: (c.fecha, c.numero_telefono or ""))
    ]


def _periodo_de_fila(nomina, fila):
    seccion = (fila.get("seccion") or "").strip()
    if seccion == "encargado":
        return nomina.fecha_inicio_encargados, nomina.fecha_fin_encargados
    if seccion == "cadena":
        return nomina.fecha_inicio_cadenas, nomina.fecha_fin_cadenas
    return nomina.fecha_inicio_asesores, nomina.fecha_fin_asesores


@router.get("/mi-recibo/detalle")
def mi_recibo_detalle(
    db: Session = Depends(get_db),
    current_user: models.Usuario = Depends(get_current_user),
):
    from sqlalchemy.orm import joinedload

    nomina = _get_nomina_publicada(db)
    fila = _get_mi_fila(nomina, current_user)

    ids = fila.get("usuario_ids", [])
    inicio, fin = _periodo_de_fila(nomina, fila)

    if not ids or not inicio or not fin:
        return {
            "disponible": False,
            "motivo": "Esta nómina no tiene periodo de comisiones registrado",
            "periodo": None,
            "accesorios": [],
            "telefonos": [],
            "chips": [],
            "incubadora": _incubadora_lineas(db, nomina, fila),
            "cuadre": None,
        }

    ventas = (
        db.query(models.Venta)
        .options(joinedload(models.Venta.comision_obj))
        .filter(
            models.Venta.empleado_id.in_(ids),
            models.Venta.cancelada == False,
            models.Venta.fecha >= inicio,
            models.Venta.fecha <= fin,
        )
        .all()
    )

    chips = (
        db.query(models.VentaChip)
        .filter(
            models.VentaChip.empleado_id.in_(ids),
            models.VentaChip.cancelada == False,
            models.VentaChip.es_incubadora == False,
            models.VentaChip.validado == True,
            models.VentaChip.numero_telefono.isnot(None),
            models.VentaChip.fecha >= inicio,
            models.VentaChip.fecha <= fin,
        )
        .all()
    )

    # Una sola llamada por request, con TODAS las cuentas del empleado.
    congelados = dias_congelados_batch(db, list(ids), inicio, fin)
    fechas_congeladas: set = set()

    grupos_acc: dict = {}
    grupos_tel: dict = {}

    for v in ventas:
        unit = _comision_unitaria(v)
        if unit <= 0:
            continue
        cant = int(v.cantidad or 0)
        if cant <= 0:
            continue

        if v.tipo_producto == "telefono":
            key = ((v.producto or "").strip(), unit, (v.tipo_venta or "").strip())
            destino = grupos_tel
        elif v.tipo_producto == "accesorios":
            key = ((v.producto or "").strip(), unit, None)
            destino = grupos_acc
        else:
            continue

        if key not in destino:
            destino[key] = {
                "producto": key[0],
                "comision_unitaria": round(unit, 2),
                "piezas": 0,
                "subtotal": 0.0,
                "piezas_congeladas": 0,
                "congelado": False,
            }
            if key[2] is not None:
                destino[key]["tipo_venta"] = key[2]

        subtotal, congelado = aplicar_congelamiento(
            unit * cant, v.fecha in congelados.get(v.empleado_id, set())
        )
        destino[key]["piezas"] += cant
        destino[key]["subtotal"] += subtotal
        if congelado:
            fechas_congeladas.add(v.fecha)
            destino[key]["piezas_congeladas"] += cant
            destino[key]["congelado"] = True

    # Agrupado por (tipo, recarga, comision) como siempre, pero cada grupo lleva
    # sus numeros con fecha: asi no se repite "Activacion $100" nueve veces y el
    # empleado sigue pudiendo identificar un chip faltante.
    grupos_chip: dict = {}
    for c in chips:
        com = float(c.comision or 0)
        if com <= 0:
            continue
        key = ((c.tipo_chip or "").strip(), float(c.monto_recarga or 0), com)
        if key not in grupos_chip:
            grupos_chip[key] = {
                "tipo_chip": key[0],
                "monto_recarga": key[1],
                "comision_unitaria": round(com, 2),
                "piezas": 0,
                "subtotal": 0.0,
                "numeros": [],
                "piezas_congeladas": 0,
                "congelado": False,
            }
        subtotal, congelado = aplicar_congelamiento(
            com, c.fecha in congelados.get(c.empleado_id, set())
        )
        grupos_chip[key]["piezas"] += 1
        grupos_chip[key]["subtotal"] += subtotal
        if congelado:
            fechas_congeladas.add(c.fecha)
            grupos_chip[key]["piezas_congeladas"] += 1
            grupos_chip[key]["congelado"] = True
        grupos_chip[key]["numeros"].append({
            "fecha": str(c.fecha),
            "numero": c.numero_telefono,
            "congelado": congelado,
        })

    def _ordenar(d):
        filas = sorted(d.values(), key=lambda x: (-x["subtotal"], x.get("producto", "")))
        for f in filas:
            f["subtotal"] = round(f["subtotal"], 2)
        return filas

    lista_acc = _ordenar(grupos_acc)
    lista_tel = _ordenar(grupos_tel)
    lista_chip = sorted(
        grupos_chip.values(), key=lambda x: (-x["subtotal"], x["tipo_chip"])
    )
    for f in lista_chip:
        f["subtotal"] = round(f["subtotal"], 2)
        f["numeros"].sort(key=lambda n: (n["fecha"], n["numero"] or ""))

    calc = {
        "accesorios": round(sum(f["subtotal"] for f in lista_acc), 2),
        "telefonos": round(sum(f["subtotal"] for f in lista_tel), 2),
        "chips": round(sum(f["subtotal"] for f in lista_chip), 2),
    }
    pagado = {
        "accesorios": round(float(fila.get("accesorios", 0) or 0), 2),
        "telefonos": round(float(fila.get("telefonos", 0) or 0), 2),
        "chips": round(float(fila.get("chips", 0) or 0), 2),
    }
    cuadra = all(abs(calc[k] - pagado[k]) < 0.01 for k in calc)

    return {
        "disponible": True,
        "motivo": None,
        "periodo": {"inicio": str(inicio), "fin": str(fin)},
        "accesorios": lista_acc,
        "telefonos": lista_tel,
        "chips": lista_chip,
        "incubadora": _incubadora_lineas(db, nomina, fila),
        "cuadre": {
            "cuadra": cuadra,
            "calculado": calc,
            "pagado": pagado,
        },
        "dias_congelados": len(fechas_congeladas),
        "congelado": bool(fechas_congeladas),
    }
