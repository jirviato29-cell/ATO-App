import enum
from sqlite3.dbapi2 import Timestamp
from sqlalchemy import BigInteger, Boolean, Column, Date, Float, Integer, JSON, Numeric, String, Text, Enum, DateTime, Time, UniqueConstraint, func
import sqlalchemy
from .database import Base
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship


class RolEnum(str, enum.Enum):
    admin = "admin"
    encargado = "encargado"
    asesor = "asesor"
    direccion = "direccion"
    check = "check"

class Usuario(Base):
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True)
    nombre_completo = Column(String, nullable=False)
    username = Column(String, unique=True, nullable=False)
    rol = Column(Enum(RolEnum), nullable=False, default=RolEnum.asesor)
    password = Column(String, nullable=False)
    # Copia cifrada con Fernet (PASSWORD_ENC_KEY) para que el admin pueda verla.
    # Nunca va en un schema de respuesta salvo GET /usuarios/{id}/password.
    password_cifrada = Column(Text, nullable=True)
    modulo_id = Column(Integer, ForeignKey("modulos.id"), nullable=True)
    is_admin = Column(Boolean, default=False)
    activo = Column(Boolean, default=True)
    ventas = relationship("Venta", back_populates="empleado")
    ventas_telefono = relationship("VentaTelefono", back_populates="empleado")
    ventas_chip = relationship("VentaChip", back_populates="empleado", foreign_keys="VentaChip.empleado_id")
    modulo = relationship("Modulo", backref="usuarios")
    sueldo_base = Column(Float, default=0)
    lugar_trabajo = Column(String, nullable=True)
    latitud_promotor = Column(Float, nullable=True)
    longitud_promotor = Column(Float, nullable=True)
    radio_metros_promotor = Column(Integer, default=100)
    forma_pago = Column(String, nullable=True)
    cuenta_clabe = Column(String, nullable=True)
    cuenta_interbancaria = Column(String, nullable=True)
    nombre_englobado = Column(String(50), nullable=True, index=True)
    jornada_fija = Column(Numeric(5, 2), default=0)
    horario_semanal = Column(JSON, default=list)
    dia_descanso = Column(String(10), nullable=True)
    tienda_id = Column(Integer, ForeignKey("tiendas.id"), nullable=True)
    asegurado = Column(Boolean, default=False, nullable=False)

    tienda = relationship("Tienda", backref="usuarios")


class Cadena(Base):
    __tablename__ = "cadenas"

    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String(20), unique=True, nullable=False)
    nombre = Column(String(100), nullable=False)
    activo = Column(Boolean, default=True, nullable=False)
    creado_en = Column(DateTime(timezone=True), server_default=func.now())


class Tienda(Base):
    __tablename__ = "tiendas"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, unique=True, nullable=False)
    activo = Column(Boolean, default=True, nullable=False)
    creado_en = Column(DateTime(timezone=True), server_default=func.now())

    cadena_id = Column(Integer, ForeignKey("cadenas.id"), nullable=True)
    num_tienda = Column(Integer, nullable=True)
    garantizados = Column(Numeric(5, 2), default=0)
    clave_1 = Column(String(30), nullable=True)
    clave_2 = Column(String(30), nullable=True)
    clave_3 = Column(String(30), nullable=True)
    clave_4 = Column(String(30), nullable=True)

    cadena = relationship("Cadena", backref="tiendas")


class TiendaClave(Base):
    __tablename__ = "tienda_claves"

    id = Column(Integer, primary_key=True, index=True)
    tienda_id = Column(Integer, ForeignKey("tiendas.id", ondelete="CASCADE"), nullable=False)
    clave = Column(String(50), unique=True, nullable=False)
    en_uso = Column(Boolean, default=False, nullable=False)
    usuario = Column(String(100), nullable=True)
    password = Column(String(200), nullable=True)
    notas = Column(Text, nullable=True)
    creado_en = Column(DateTime(timezone=True), server_default=func.now())
    actualizado_en = Column(DateTime(timezone=True), server_default=func.now())

    tienda = relationship("Tienda", backref="claves")


class AsistenciaLegacy(Base):
    __tablename__ = "asistencias"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, nullable=False)
    modulo = Column(String, nullable=False)
    turno = Column(String, nullable=False)
    fecha = Column(Date, default=func.current_date())
    hora = Column(Time, default=func.current_time())
    hora_salida = Column(Time, nullable=True)


class Asistencia(Base):
    __tablename__ = "asistencia"
    __table_args__ = (
        UniqueConstraint("usuario_id", "fecha", "tipo", name="uq_asistencia_usuario_fecha_tipo"),
    )

    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    username = Column(String, nullable=False)
    modulo_id = Column(Integer, ForeignKey("modulos.id"), nullable=True)
    fecha = Column(Date, nullable=False)
    tipo = Column(String(10), nullable=False)
    hora = Column(DateTime(timezone=True), nullable=True)
    latitud = Column(Float, nullable=True)
    longitud = Column(Float, nullable=True)
    foto_url = Column(String, nullable=True)
    dentro_de_zona = Column(Boolean, nullable=True)
    distancia_metros = Column(Float, nullable=True)
    creada_at = Column(DateTime(timezone=True), server_default=func.now())

    usuario = relationship("Usuario", foreign_keys=[usuario_id])
    modulo_rel = relationship("Modulo", foreign_keys=[modulo_id])


class DiaDescanso(Base):
    __tablename__ = "dias_descanso"
    __table_args__ = (
        UniqueConstraint("usuario_id", "fecha", name="uq_dias_descanso_usuario_fecha"),
    )

    id = Column(BigInteger, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    username = Column(Text, nullable=False)
    modulo_id = Column(Integer, nullable=True)
    fecha = Column(Date, nullable=False, index=True)
    creada_at = Column(DateTime(timezone=True), server_default=func.now())
    cancelado_at = Column(DateTime(timezone=True), nullable=True)
    cancelado_por = Column(Integer, ForeignKey("usuarios.id"), nullable=True)

    usuario = relationship("Usuario", foreign_keys=[usuario_id])
    cancelador = relationship("Usuario", foreign_keys=[cancelado_por])


class NotificacionAsistencia(Base):
    __tablename__ = "notificaciones_asistencia"

    id = Column(Integer, primary_key=True, index=True)
    asistencia_id = Column(Integer, ForeignKey("asistencia.id"), nullable=True)
    usuario_id = Column(Integer, nullable=False)
    username = Column(String, nullable=False)
    modulo_id = Column(Integer, nullable=True)
    mensaje = Column(String, nullable=False)
    distancia_metros = Column(Float, nullable=True)
    leida = Column(Boolean, default=False)
    creada_at = Column(DateTime(timezone=True), server_default=func.now())


class Venta(Base):
    __tablename__ = "ventas"

    id = Column(Integer, primary_key=True, index=True)
    empleado_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    modulo_id = Column(Integer, ForeignKey("modulos.id"), nullable=False)
    producto = Column(String, nullable=False)
    cantidad = Column(Integer, nullable=False)
    precio_unitario = Column(Float, nullable=False)
    tipo_venta = Column(String, nullable=False)  
    total = Column(Float, nullable=True) 
    comision_id    = Column(Integer, ForeignKey("comisions.id"), nullable=True)
    comision_monto = Column(Float, nullable=True)
    metodo_pago = Column(String)
    cancelada = Column(Boolean, default=False)
    chip_casado = Column(String, nullable=True)  # Para relacionar venta de teléfono con venta de chip
    fecha = Column(Date, default=func.current_date())
    hora = Column(Time, default=func.current_time())
    telefono_cliente = Column(String, nullable=True)
    tipo_producto = Column(String, nullable=False)
    folio = Column(String, nullable=True)
    imei = Column(String, nullable=True)
    clasificacion = Column(String, nullable=True)
    devuelta = Column(Boolean, default=False)
    fecha_devolucion = Column(Date, nullable=True)
    monto_devuelto = Column(Float, nullable=True)
    devuelta_por = Column(Integer, nullable=True)

    empleado = relationship("Usuario", back_populates="ventas")
    comision_obj = relationship("Comision")
    modulo = relationship("Modulo", back_populates="ventas")

class VentaChip(Base):
    __tablename__ = "venta_chips"

    id = Column(Integer, primary_key=True, index=True)
    empleado_id = Column(Integer, ForeignKey("usuarios.id"))
    tipo_chip = Column(String, nullable=False)
    numero_telefono = Column(String, nullable=False)
    monto_recarga = Column(Float, nullable=False)
    comision = Column(Float, nullable=True)
    cvip = Column(Boolean, default=False)
    fecha = Column(Date, nullable=False)
    hora = Column(Time, nullable=False)
    cancelada = Column(Boolean, default=False)
    validado = Column(Boolean, default=False)
    comision_pagada = Column(Boolean, default=False)
    descripcion_rechazo = Column(String, nullable=True)
    es_incubadora = Column(Boolean, default=False,  nullable=False)
    fecha_validacion = Column(DateTime(timezone=True), nullable=True)
    imei = Column(String, nullable=True)
    iccid = Column(String, nullable=True)
    curp = Column(String, nullable=True)
    nip = Column(String, nullable=True)
    portabilidad_realizada = Column(Boolean, default=False, nullable=False)
    fecha_portabilidad = Column(DateTime, nullable=True)
    cambio_chip = Column(Boolean, default=False, nullable=False)

    empleado = relationship("Usuario", foreign_keys=[empleado_id])


class Comision(Base):
    __tablename__ = "comisions"

    id = Column(Integer, primary_key=True, index=True)
    producto = Column(String, unique=True, nullable=False)
    cantidad = Column(Float, nullable=False)  

    activo = Column(Boolean, default=True)




class EstadoTraspasoEnum(str, enum.Enum):
    pendiente = "pendiente"
    aprobado = "aprobado"
    rechazado = "rechazado"

class Traspaso(Base):
    __tablename__ = "traspasos"

    id = Column(Integer, primary_key=True, index=True)
    producto = Column(String, nullable=False)
    clave = Column(String, nullable=False)
    precio = Column(Integer, nullable=False)
    cantidad = Column(Integer, nullable=False)
    tipo_producto = Column(String, nullable=False)
    modulo_origen = Column(String, nullable=False)
    modulo_destino = Column(String, nullable=False)
    estado = Column(Enum(EstadoTraspasoEnum), default=EstadoTraspasoEnum.pendiente)
    fecha = Column(DateTime(timezone=True),default=lambda: datetime.now(timezone.utc),nullable=False)
    solicitado_por = Column(Integer, ForeignKey("usuarios.id"))
    aprobado_por = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    visible_en_pendientes = Column(Boolean, default=True)
    solicitante = relationship("Usuario", foreign_keys=[solicitado_por])
    aprobador = relationship("Usuario", foreign_keys=[aprobado_por])
    folio = Column(String(50), nullable=True)
    imei = Column(String(20), nullable=True)


class InventarioGeneral(Base):
    __tablename__ = "inventario_general"

    id = Column(Integer, primary_key=True, index=True)
    cantidad = Column(Integer, nullable=False)
    clave = Column(String, unique=True, nullable=False) 
    producto = Column(String, nullable=False)
    precio = Column(Integer, nullable=True)
    tipo_producto = Column(String, nullable=False)  

class InventarioModulo(Base):
    __tablename__ = "inventario_modulo"

    id = Column(Integer, primary_key=True, index=True)
    cantidad = Column(Integer, nullable=False)
    clave = Column(String, nullable=False) 
    producto = Column(String, nullable=False)
    precio = Column(Integer, nullable=False)
    modulo_id = Column(Integer, ForeignKey("modulos.id"))
    tipo_producto = Column(String, nullable=False)
    
    modulo = relationship("Modulo")
    
    
    
    
class CorreoPromocional(Base):
    __tablename__ = "correos_promocionales"

    id = Column(Integer, primary_key=True, index=True)
    correo = Column(String, unique=True, nullable=False)
    fecha_registro = Column(DateTime, default=datetime.utcnow)



class Modulo(Base):
    __tablename__ = "modulos"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, unique=True, nullable=False)
    latitud = Column(Float, nullable=True)
    longitud = Column(Float, nullable=True)
    radio_metros = Column(Integer, default=100)
    activo = Column(Boolean, default=True)
    congelado = Column(Boolean, default=False, nullable=False)

    ventas = relationship("Venta", back_populates="modulo")
    cortes = relationship("CorteDia", back_populates="modulo")

class VentaTelefono(Base):
    __tablename__ = "venta_telefonos"

    id = Column(Integer, primary_key=True, index=True)
    empleado_id = Column(Integer, ForeignKey("usuarios.id"))
    marca = Column(String, nullable=False)
    modelo = Column(String, nullable=False)
    tipo = Column(String, nullable=False)
    precio_venta = Column(Float, nullable=False)
    metodo_pago = Column(String)
    fecha = Column(Date, default=date.today)
    hora = Column(Time, default=datetime.now().time)
    cancelada = Column(Boolean, default=False)
    modulo_id = Column(Integer, ForeignKey("modulos.id"))

    empleado = relationship("Usuario")


class InventarioTelefono(Base):
    __tablename__ = "inventario_telefonos"

    id = Column(Integer, primary_key=True, index=True)
    marca = Column(String)
    modelo = Column(String)
    cantidad = Column(Integer)
    precio = Column(Float)
    modulo_id = Column(Integer, ForeignKey("modulos.id"))

    modulo = relationship("Modulo")
    

class InventarioTelefonoGeneral(Base):
    __tablename__ = "inventario_telefonos_general"

    id = Column(Integer, primary_key=True, index=True)
    marca = Column(String)
    modelo = Column(String)
    cantidad = Column(Integer)
    precio = Column(Float)
    clave = Column(String, unique=True, nullable=False) 



class CorteDia(Base):
    __tablename__ = "cortes_dia"

    id = Column(Integer, primary_key=True, index=True)
    fecha = Column(Date, default=func.current_date())
    modulo_id = Column(Integer, ForeignKey("modulos.id"), nullable=False)

    # 🔹 Subtotales productos
    accesorios_efectivo = Column(Float, default=0)
    accesorios_tarjeta = Column(Float, default=0)
    accesorios_total = Column(Float, default=0)

    # 🔹 Subtotales teléfonos
    telefonos_efectivo = Column(Float, default=0)
    telefonos_tarjeta = Column(Float, default=0)
    telefonos_total = Column(Float, default=0)

    # 🔹 Totales globales
    total_efectivo = Column(Float, default=0)
    total_tarjeta = Column(Float, default=0)
    total_sistema = Column(Float, default=0)
    total_general = Column(Float, default=0)

    # 🔹 Adicionales
    adicional_recargas = Column(Float, default=0)
    adicional_transporte = Column(Float, default=0)
    adicional_otros = Column(Float, default=0)
    adicional_mayoreo = Column(Float, default=0)
    adicional_mayoreo_para = Column(String, nullable=True)

    # 🔹 Salida y estado
    salida_efectivo = Column(Float, default=0)
    nota_salida = Column(String, nullable=True)
    enviado = Column(Boolean, default=False)

    # 🔹 Revisión dirección
    revisado_direccion = Column(Boolean, default=False, nullable=False)
    revisado_por = Column(String, nullable=True)
    revisado_at = Column(DateTime(timezone=True), nullable=True)
    recarga_revisada = Column(Boolean, default=False)

    modulo = relationship("Modulo", back_populates="cortes")
    
    


class InventarioFisico(Base):
    __tablename__ = "inventario_fisico"

    id = Column(Integer, primary_key=True, index=True)
    producto = Column(String, index=True)
    clave = Column(String, index=True)
    cantidad = Column(Integer, nullable=False)
    fecha = Column(DateTime, default=datetime.utcnow)
    

class InventarioTelefonoFisico(Base):
    __tablename__ = "inventario_telefonos_fisico"

    id = Column(Integer, primary_key=True, index=True)
    marca = Column(String)
    modelo = Column(String)
    clave = Column(String)
    cantidad = Column(Integer)
    fecha = Column(DateTime, default=datetime.utcnow)



class NominaPeriodo(Base):
    __tablename__ = "nomina_periodo"

    id = Column(Integer, primary_key=True, index=True)
    fecha_inicio = Column(Date, nullable=False)
    fecha_fin = Column(Date, nullable=False)

    inicio_a = Column(Date, nullable=True)
    fin_a = Column(Date, nullable=True)

    inicio_c = Column(Date, nullable=True)
    fin_c = Column(Date, nullable=True)

    activa = Column(Boolean, default=False)
    estado = Column(String(20), default="abierta")  # abierta | congelada | pagada
    creado = Column(DateTime, server_default=func.now())


class NominaEmpleado(Base):
    __tablename__ = "nomina_empleados"

    id = Column(Integer, primary_key=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    periodo_id = Column(Integer, ForeignKey("nomina_periodo.id"))
    total_comisiones = Column(Integer, default=0)
    horas_extra = Column(Float, default=0)
    pago_horas_extra = Column(Float, default=0)
    precio_hora_extra = Column(Float, default=0)
    sanciones = Column(Float, default=0)
    comisiones_pendientes = Column(Float, default=0)
    total_pagar = Column(Float, default=0)


class NominaHistorial(Base):
    __tablename__ = "nomina_historial"
    __table_args__ = (
        UniqueConstraint("semana_inicio", "usuario_id", name="uq_historial_semana_usuario"),
    )

    id = Column(Integer, primary_key=True, index=True)
    semana_inicio = Column(Date, nullable=False)
    semana_fin = Column(Date, nullable=False)
    comisiones_inicio = Column(Date, nullable=False)
    comisiones_fin = Column(Date, nullable=False)
    usuario_id = Column(Integer, nullable=False)
    username = Column(String, nullable=False)
    grupo = Column(String(1), nullable=False)
    comisiones_accesorios = Column(Float, default=0)
    comisiones_telefonos = Column(Float, default=0)
    comisiones_chips = Column(Float, default=0)
    comisiones_total = Column(Float, default=0)
    sueldo_base = Column(Float, default=0)
    horas_extra = Column(Integer, default=0)
    precio_hora_extra = Column(Float, default=0)
    pago_horas_extra = Column(Float, default=0)
    sanciones = Column(Float, default=0)
    comisiones_pendientes = Column(Float, default=0)
    horas_faltantes = Column(Float, default=0)
    total_pagar = Column(Float, default=0)
    guardado_at = Column(DateTime(timezone=True), server_default=func.now())



class TipoMovimientoEnum(str, enum.Enum):
    VENTA = "VENTA"
    ENTRADA = "ENTRADA"
    TRASPASO_ENTRADA = "TRASPASO_ENTRADA"
    TRASPASO_SALIDA = "TRASPASO_SALIDA"
    AJUSTE_POSITIVO = "AJUSTE_POSITIVO"
    AJUSTE_NEGATIVO = "AJUSTE_NEGATIVO"
    CANCELACION_VENTA = "CANCELACION_VENTA"
    AJUSTE_INVENTARIO = "AJUSTE_INVENTARIO"
    CONTEO_FISICO = "CONTEO_FISICO"

    

class KardexMovimiento(Base):
    __tablename__ = "kardex_movimientos"

    id = Column(Integer, primary_key=True, index=True)

    producto = Column(String, nullable=False)
    tipo_producto = Column(String, nullable=False)

    cantidad = Column(Integer, nullable=False)

    tipo_movimiento = Column(Enum(TipoMovimientoEnum, name="tipo_movimiento_enum"), nullable=False)

    modulo_origen_id = Column(Integer, ForeignKey("modulos.id"), nullable=True)
    modulo_destino_id = Column(Integer, ForeignKey("modulos.id"), nullable=True)

    referencia_id = Column(Integer, nullable=True)
    # id de venta o traspaso si aplica

    usuario_id = Column(Integer, ForeignKey("usuarios.id"))

    fecha = Column(DateTime(timezone=True), server_default=func.now())


class Plan(Base):
    __tablename__ = "planes"

    id = Column(Integer, primary_key=True, index=True)
    tipo_tramite = Column(String, nullable=False)
    tipo_plan = Column(String, nullable=False)

    empleado_id = Column(Integer, ForeignKey("usuarios.id"))
    modulo_id = Column(Integer, ForeignKey("modulos.id"))
    fecha_inicio = Column(Date, default=date.today)
    fecha = Column(DateTime, default=datetime.utcnow)


class EntradaMercancia(Base):
    __tablename__ = "entradas_mercancia"

    id = Column(Integer, primary_key=True, index=True)
    folio = Column(String(20), nullable=False, unique=True)
    modulo_id = Column(Integer, ForeignKey("modulos.id"), nullable=False)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    fecha = Column(DateTime(timezone=True), nullable=False)


class ComisionModulo(Base):
    __tablename__ = "comision_modulos"

    id = Column(Integer, primary_key=True, index=True)
    modulo = Column(String, unique=True, nullable=False)
    porcentaje = Column(Numeric(5, 2), nullable=False, default=0)


class JornadaAsistencia(Base):
    __tablename__ = "jornada_asistencia"
    __table_args__ = (
        UniqueConstraint("usuario_id", "ciclo_inicio", name="uq_jornada_usuario_ciclo"),
    )

    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    ciclo_inicio = Column(Date, nullable=False)
    horas = Column(Numeric(6, 2), nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class CajaChica(Base):
    __tablename__ = "caja_chica"

    id = Column(Integer, primary_key=True)
    modulo_id = Column(Integer, ForeignKey("modulos.id"), nullable=False)
    fecha = Column(Date, nullable=False)
    monto = Column(Numeric(10, 2), nullable=False, default=0)
    creado_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(ZoneInfo("America/Mexico_City"))
    )
    actualizado_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(ZoneInfo("America/Mexico_City")),
        onupdate=lambda: datetime.now(ZoneInfo("America/Mexico_City"))
    )

    __table_args__ = (
        UniqueConstraint("modulo_id", "fecha", name="caja_chica_modulo_fecha_unique"),
    )


class CicloGuardado(Base):
    __tablename__ = "ciclos_guardados"

    id = Column(Integer, primary_key=True, index=True)
    concepto = Column(String(50), nullable=False, index=True)
    etiqueta = Column(String(100), nullable=False)
    fecha_inicio = Column(Date, nullable=False)
    fecha_fin = Column(Date, nullable=False)
    datos = Column(JSON, nullable=False, default=list)
    creado_por = Column(String(100), nullable=False)
    creado_en = Column(DateTime(timezone=True), server_default=func.now())


class Nomina(Base):
    __tablename__ = "nominas"

    id = Column(Integer, primary_key=True, index=True)
    etiqueta = Column(String(200), nullable=False)
    ciclo_horas_extras_id = Column(Integer, ForeignKey("ciclos_guardados.id"), nullable=True)
    ciclo_comisiones_id = Column(Integer, ForeignKey("ciclos_guardados.id"), nullable=True)
    ciclo_sueldos_encargados_id = Column(Integer, ForeignKey("ciclos_guardados.id"), nullable=True)
    fecha_inicio_asesores = Column(Date, nullable=True)
    fecha_fin_asesores = Column(Date, nullable=True)
    fecha_inicio_encargados = Column(Date, nullable=True)
    fecha_fin_encargados = Column(Date, nullable=True)
    fecha_inicio_cadenas = Column(Date, nullable=True)
    fecha_fin_cadenas = Column(Date, nullable=True)
    total_pago = Column(Numeric(12, 2), nullable=False, default=0)
    datos = Column(JSON, nullable=False, default=list)
    publicada = Column(Boolean, default=False, nullable=False)
    creado_por = Column(String(100), nullable=False)
    creado_en = Column(DateTime(timezone=True), server_default=func.now())

    ciclo_horas_extras = relationship("CicloGuardado", foreign_keys=[ciclo_horas_extras_id])
    ciclo_comisiones = relationship("CicloGuardado", foreign_keys=[ciclo_comisiones_id])
    ciclo_sueldos_encargados = relationship("CicloGuardado", foreign_keys=[ciclo_sueldos_encargados_id])


class NominaIncubadora(Base):
    __tablename__ = "nominas_incubadora"

    id         = Column(Integer, primary_key=True, index=True)
    etiqueta   = Column(String(200), nullable=False)
    total_pago = Column(Numeric(12, 2), nullable=False, default=0)
    datos      = Column(JSON, nullable=False, default=list)
    creado_por = Column(String(100), nullable=False)
    creado_en  = Column(DateTime(timezone=True), server_default=func.now())


class AjusteInventario(Base):
    __tablename__ = "ajustes_inventario"

    id        = Column(Integer, primary_key=True, index=True)
    folio     = Column(String(20), nullable=False, unique=True)
    modulo_id = Column(Integer, ForeignKey("modulos.id"), nullable=False)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    fecha     = Column(DateTime(timezone=True), nullable=False)
    motivo    = Column(String, nullable=True)

    modulo  = relationship("Modulo")
    usuario = relationship("Usuario", foreign_keys=[usuario_id])
    items   = relationship("AjusteInventarioItem", back_populates="ajuste", cascade="all, delete-orphan")


class AjusteInventarioItem(Base):
    __tablename__ = "ajustes_inventario_items"

    id                = Column(Integer, primary_key=True, index=True)
    ajuste_id         = Column(Integer, ForeignKey("ajustes_inventario.id"), nullable=False)
    producto          = Column(String, nullable=False)
    clave             = Column(String, nullable=False)
    cantidad_anterior = Column(Integer, nullable=False)
    cantidad_nueva    = Column(Integer, nullable=False)
    delta             = Column(Integer, nullable=False)

    ajuste = relationship("AjusteInventario", back_populates="items")
    creado_en  = Column(DateTime(timezone=True), server_default=func.now())


class ConteoFisico(Base):
    __tablename__ = "conteos_fisicos"

    id                     = Column(Integer, primary_key=True, index=True)
    folio                  = Column(String(20), unique=True, nullable=False)
    modulo                 = Column(String(100), nullable=False)
    modulo_id              = Column(Integer, ForeignKey("modulos.id"), nullable=True)
    fecha                  = Column(DateTime(timezone=True), server_default=func.now())
    usuario                = Column(String(100), nullable=True)
    archivo_nombre         = Column(String(255), nullable=True)
    total_filas            = Column(Integer, nullable=True)
    productos_actualizados = Column(Integer, nullable=True)
    productos_creados      = Column(Integer, nullable=True)
    productos_en_cero      = Column(Integer, nullable=True)
    estado                 = Column(String(20), default="aplicado")
    notas                  = Column(String, nullable=True)

    items = relationship("ConteoFisicoItem", back_populates="conteo", cascade="all, delete-orphan")
    imeis = relationship("ConteoFisicoImei", cascade="all, delete-orphan")


class ConteoFisicoItem(Base):
    __tablename__ = "conteos_fisicos_items"

    id                = Column(Integer, primary_key=True, index=True)
    conteo_id         = Column(Integer, ForeignKey("conteos_fisicos.id", ondelete="CASCADE"), nullable=False)
    clave             = Column(String(50), nullable=True)
    producto          = Column(String(255), nullable=True)
    cantidad_anterior = Column(Integer, nullable=True)
    cantidad_nueva    = Column(Integer, nullable=True)
    accion            = Column(String(20), nullable=True)
    producto_creado   = Column(Boolean, default=False)

    conteo = relationship("ConteoFisico", back_populates="items")


class ConteoFisicoImei(Base):
    __tablename__ = "conteos_fisicos_imeis"

    id                = Column(Integer, primary_key=True, index=True)
    conteo_id         = Column(Integer, ForeignKey("conteos_fisicos.id", ondelete="CASCADE"), nullable=False)
    imei              = Column(String(20), nullable=False, index=True)
    equipo_id         = Column(Integer, ForeignKey("equipos_telcel.id", ondelete="SET NULL"), nullable=True)
    clave             = Column(String(100), nullable=True)
    producto          = Column(String(255), nullable=True)
    estatus_sistema   = Column(String(30), nullable=True)
    modulo_sistema_id = Column(Integer, nullable=True)
    resultado         = Column(String(30), nullable=False)
    fecha             = Column(DateTime, nullable=True)


class PlanTarifario(Base):
    __tablename__ = "planes_tarifarios"

    id                 = Column(Integer, primary_key=True, index=True)
    fecha              = Column(DateTime, nullable=True)
    empleado_id        = Column(Integer, nullable=True)
    modulo_id          = Column(Integer, nullable=True)
    tipo_plan          = Column(String, nullable=True)
    estatus            = Column(String, nullable=True)
    categoria          = Column(String, nullable=True)
    clasificacion      = Column(String, nullable=True)
    equipo             = Column(String, nullable=True)
    imei               = Column(String, nullable=True)
    precio_equipo      = Column(Numeric, nullable=True)
    plazo              = Column(Integer, nullable=True)
    linea              = Column(String, nullable=True)
    cuenta             = Column(String, nullable=True)
    pago_inicial         = Column(Boolean, default=False)
    monto_pago_inicial   = Column(Numeric, default=0)
    metodo_pago_inicial  = Column(String, nullable=True)
    pagado               = Column(Boolean, default=False)
    fecha_pago           = Column(DateTime, nullable=True)
    contrato_listo       = Column(Boolean, default=False)
    venta_pi_id          = Column(Integer, nullable=True)


# ── Módulo de Gastos ──────────────────────────────────────────────────────────

class Gasto(Base):
    __tablename__ = "gastos"

    id = Column(Integer, primary_key=True, index=True)
    tipo = Column(String, nullable=False)  # 'personal' | 'iglesia' | 'prestamos'
    concepto = Column(String, nullable=False)
    monto = Column(Numeric, nullable=False, default=0)
    estado = Column(String, nullable=False, default="pendiente")  # 'pendiente' | 'pagado'
    fecha_registro = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(ZoneInfo("America/Mexico_City")),
    )
    fecha_pago = Column(DateTime(timezone=True), nullable=True)

    abonos = relationship(
        "GastoAbono",
        back_populates="gasto",
        cascade="all, delete-orphan",
        order_by="GastoAbono.fecha",
    )


class GastoAbono(Base):
    __tablename__ = "gastos_abonos"

    id = Column(Integer, primary_key=True, index=True)
    gasto_id = Column(Integer, ForeignKey("gastos.id", ondelete="CASCADE"), nullable=False)
    monto = Column(Numeric, nullable=False, default=0)
    fecha = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(ZoneInfo("America/Mexico_City")),
    )
    nota = Column(String, nullable=True)

    gasto = relationship("Gasto", back_populates="abonos")


class JustificacionAsistencia(Base):
    __tablename__ = "justificaciones_asistencia"

    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    fecha = Column(Date, nullable=False)
    estado = Column(String(20), nullable=False)   # 'falta'|'justificada'|'vacaciones'
    nota = Column(Text, nullable=True)
    creado_por = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    creado_en = Column(DateTime(timezone=True), server_default=func.now())


class SolicitudCorreccionAsistencia(Base):
    __tablename__ = "solicitudes_correccion_asistencia"

    id = Column(Integer, primary_key=True, index=True)
    asistencia_id = Column(
        BigInteger,
        ForeignKey("asistencia.id", ondelete="CASCADE"),
        nullable=True,
    )
    usuario_id = Column(Integer, nullable=False)
    username = Column(String, nullable=False)
    fecha = Column(Date, nullable=False)
    tipo = Column(String, nullable=False)
    motivo = Column(Text, nullable=True)
    estado = Column(String, nullable=False, server_default="pendiente")
    resuelto_por = Column(Integer, nullable=True)
    resuelto_en = Column(DateTime(timezone=True), nullable=True)
    nota_admin = Column(Text, nullable=True)
    creado_en = Column(DateTime(timezone=True), server_default=func.now())


class EquiposTelcel(Base):
    __tablename__ = "equipos_telcel"

    id = Column(Integer, primary_key=True, index=True)
    imei = Column(String(20), nullable=False, unique=True)
    clave = Column(String, nullable=False)
    producto = Column(String, nullable=False)
    fecha_compra = Column(Date, nullable=False)
    estatus = Column(String(20), nullable=False, server_default="en_bodega")
    modulo_id = Column(Integer, nullable=True)
    fecha_salida = Column(DateTime, nullable=True)
    folio = Column(String, nullable=True)
    fecha_venta = Column(DateTime, nullable=True)
    folio_venta = Column(String, nullable=True)
    activado = Column(Boolean, nullable=False, server_default="false")
    estado_activacion = Column(String(20), nullable=False, server_default="no_activado")
    fecha_activacion = Column(Date, nullable=True)
    fecha_estatus_inicial = Column(Date, nullable=True)
    cumple_arl = Column(Boolean, nullable=False, server_default="false")


class Devolucion(Base):
    __tablename__ = "devoluciones"

    id = Column(Integer, primary_key=True, index=True)
    venta_id = Column(Integer, ForeignKey("ventas.id"), nullable=False)
    folio = Column(String(50), nullable=True)
    modulo_id = Column(Integer, nullable=False)
    fecha = Column(Date, nullable=False)
    monto = Column(Float, nullable=False)
    motivo = Column(Text, nullable=True)
    usuario_id = Column(Integer, nullable=False)
    creado_at = Column(DateTime, nullable=True)


class CapturaTelcel(Base):
    __tablename__ = "capturas_telcel"
    __table_args__ = (
        UniqueConstraint("usuario_id", "fecha", "tipo",
                         name="uq_captura_usuario_fecha_tipo"),
    )

    id = Column(BigInteger, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    username = Column(Text, nullable=False)
    modulo_id = Column(Integer, nullable=True)
    tienda_id = Column(Integer, nullable=True)
    fecha = Column(Date, nullable=False)
    tipo = Column(Text, nullable=False)
    clave = Column(String(30), nullable=False)
    hora_entrada = Column(Time, nullable=True)
    hora_salida = Column(Time, nullable=True)
    duracion_minutos = Column(Integer, nullable=True)
    foto_url = Column(Text, nullable=False)
    json_ia = Column(JSONB, nullable=True)
    subido_at = Column(DateTime(timezone=True), server_default=func.now())
    creada_at = Column(DateTime(timezone=True), server_default=func.now())

    usuario = relationship("Usuario")


class DescongelacionComision(Base):
    __tablename__ = "descongelaciones_comision"

    id = Column(BigInteger, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    fecha = Column(Date, nullable=False, index=True)
    motivo = Column(Text, nullable=False)
    autorizado_por = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    creado_en = Column(DateTime(timezone=True), server_default=func.now())
    cancelado_en = Column(DateTime(timezone=True), nullable=True)
    cancelado_por = Column(Integer, ForeignKey("usuarios.id"), nullable=True)

    usuario = relationship("Usuario", foreign_keys=[usuario_id])
    autorizador = relationship("Usuario", foreign_keys=[autorizado_por])
    cancelador = relationship("Usuario", foreign_keys=[cancelado_por])
