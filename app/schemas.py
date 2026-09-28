from enum import Enum
from typing import Dict, List, Literal, Optional
from pydantic import BaseModel
from datetime import date, datetime, time
from app.models import EstadoTraspasoEnum, RolEnum
from typing import Literal


class AsistenciaLegacyBase(BaseModel):
    nombre: str
    modulo: str
    turno: str

class AsistenciaLegacyCreate(AsistenciaLegacyBase):
    pass

class AsistenciaLegacyResponse(AsistenciaLegacyBase):
    id: int
    fecha: date
    hora: time
    hora_salida: time | None

    class Config:
        from_attributes = True


# ── Schemas de Asistencia geolocalizada ──────────────────────────────────────

class AsistenciaCreate(BaseModel):
    tipo: Literal["entrada", "salida"]
    latitud: float
    longitud: float
    foto_base64: str


class AsistenciaResponse(BaseModel):
    id: int
    usuario_id: int
    username: str
    modulo_id: Optional[int] = None
    fecha: date
    tipo: str
    hora: Optional[datetime] = None
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    foto_url: Optional[str] = None
    dentro_de_zona: Optional[bool] = None
    distancia_metros: Optional[float] = None
    creada_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AsistenciaResumenDia(BaseModel):
    fecha: date
    entrada: Optional[datetime] = None
    salida: Optional[datetime] = None
    entrada_id: Optional[int] = None
    salida_id: Optional[int] = None
    horas_trabajadas: float = 0.0
    foto_entrada_url: Optional[str] = None
    foto_salida_url: Optional[str] = None
    dentro_de_zona_entrada: Optional[bool] = None
    dentro_de_zona_salida: Optional[bool] = None
    distancia_metros_entrada: Optional[float] = None
    distancia_metros_salida: Optional[float] = None
    username: Optional[str] = None
    modulo_id: Optional[int] = None
    modulo_nombre: Optional[str] = None
    lugar_trabajo: Optional[str] = None


class JustificacionCreate(BaseModel):
    usuario_id: int
    fecha: date
    estado: Literal["falta", "justificada", "vacaciones"]
    nota: str | None = None


class SolicitudCorreccionCreate(BaseModel):
    asistencia_id: int
    motivo: str


class SolicitudCorreccionResolver(BaseModel):
    aprobar: bool
    nota_admin: str | None = None


class SolicitudCorreccionResponse(BaseModel):
    id: int
    asistencia_id: Optional[int] = None
    usuario_id: int
    username: str
    fecha: date
    tipo: str
    motivo: Optional[str] = None
    estado: str
    resuelto_por: Optional[int] = None
    resuelto_en: Optional[datetime] = None
    nota_admin: Optional[str] = None
    creado_en: Optional[datetime] = None

    class Config:
        from_attributes = True


class ModuloUbicacionUpdate(BaseModel):
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    radio_metros: int = 100


class NotificacionResponse(BaseModel):
    id: int
    asistencia_id: Optional[int] = None
    usuario_id: int
    username: str
    modulo_id: Optional[int] = None
    mensaje: str
    distancia_metros: Optional[float] = None
    leida: bool
    creada_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ModuloConUbicacion(BaseModel):
    id: int
    nombre: str
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    radio_metros: int = 100

    class Config:
        from_attributes = True


class PromotorUbicacionUpdate(BaseModel):
    lugar_trabajo: str
    latitud_promotor: float
    longitud_promotor: float
    radio_metros_promotor: int = 100


class PromotorConUbicacion(BaseModel):
    id: int
    username: str
    lugar_trabajo: Optional[str] = None
    latitud_promotor: Optional[float] = None
    longitud_promotor: Optional[float] = None
    radio_metros_promotor: Optional[int] = 100

    class Config:
        from_attributes = True



class RolEnum(str, Enum):
    admin = "admin"
    encargado = "encargado"
    asesor = "asesor"
    direccion = "direccion"
    check = "check"

# 👉 Este es el que se usa para crear un usuario
class UsuarioCreate(BaseModel):
    nombre_completo: str
    username: str
    rol: RolEnum
    password: str
    modulo_id: Optional[int] = None
    is_admin: Optional[bool] = False
    forma_pago: Optional[str] = None
    cuenta_clabe: Optional[str] = None
    cuenta_interbancaria: Optional[str] = None
    nombre_englobado: Optional[str] = None
    jornada_fija: Optional[float] = 0
    horario_semanal: Optional[List[Dict]] = []
    dia_descanso: Optional[str] = None
    tienda_id: Optional[int] = None

# 👉 Este es para actualizar un usuario
class UsuarioUpdate(BaseModel):
    nombre_completo: Optional[str] = None
    username: Optional[str] = None
    rol: Optional[str] = None
    modulo_id: Optional[int] = None
    is_admin: Optional[bool] = None
    password: Optional[str] = None
    sueldo_base: Optional[float] = None
    forma_pago: Optional[str] = None
    cuenta_clabe: Optional[str] = None
    cuenta_interbancaria: Optional[str] = None
    nombre_englobado: Optional[str] = None
    jornada_fija: Optional[float] = None
    horario_semanal: Optional[List[Dict]] = None
    dia_descanso: Optional[str] = None
    tienda_id: Optional[int] = None
    asegurado: Optional[bool] = None

# 👉 Este para devolver la respuesta
class ModuloOut(BaseModel):
    id: int
    nombre: str

    class Config:
       from_attributes = True

class UsuarioResponse(BaseModel):
    id: int
    nombre_completo: Optional[str] = None
    username: str
    rol: RolEnum
    is_admin: bool
    sueldo_base: float = 0
    modulo: Optional[ModuloOut] = None
    forma_pago: Optional[str] = None
    cuenta_clabe: Optional[str] = None
    cuenta_interbancaria: Optional[str] = None
    nombre_englobado: Optional[str] = None
    jornada_fija: Optional[float] = 0
    horario_semanal: Optional[List[Dict]] = []
    dia_descanso: Optional[str] = None
    tienda_id: Optional[int] = None
    tienda: Optional["TiendaResponse"] = None
    asegurado: Optional[bool] = False

    class Config:
        from_attributes = True


class VentaCreate(BaseModel):
    producto: str
    precio_unitario: float
    cantidad: int
    tipo_producto: str 
    tipo_venta: str
    metodo_pago: str
    chip_casado: str
    telefono_cliente: Optional[str] = None



class SueldoBaseUpdate(BaseModel):
    sueldo_base: float
     

class VentaResponse(VentaCreate):
    id: int
    empleado: Optional[UsuarioResponse] = None
    modulo: Optional[ModuloOut]
    producto: str
    cantidad: int
    precio_unitario: float
    total: Optional[float] = None
    comision: Optional[float] = None
    tipo_producto: Optional[str] = None
    tipo_venta: Optional[str] = None
    metodo_pago: Optional[str] = None
    cancelada : Optional[bool] = None
    telefono_cliente: Optional[str] = None
    chip_casado: Optional[str] = None
    folio: Optional[str] = None
    fecha: date
    hora: time
    devuelta: Optional[bool] = False
    fecha_devolucion: Optional[date] = None
    monto_devuelto: Optional[float] = None

    class Config:

        from_attributes = True


class VentaCancelada(BaseModel):
    id: int
    cancelada: bool
    fecha_cancelacion: datetime

    class Config:
        from_attributes = True
        
        

class ProductoEnVenta(BaseModel):
    producto: str
    cantidad: int
    precio_unitario: float
    chip_casado: Optional[str] = None
    tipo_producto: Optional[str] = None
    tipo_venta: Optional[str] = None
    metodo_pago: Optional[str] = None
    cancelada: Optional[bool] = False
    skip_comision: Optional[bool] = False
    skip_inventario: Optional[bool] = False
    imei: Optional[str] = None
    clasificacion: Optional[str] = None

class VentaMultipleCreate(BaseModel):
    productos: List[ProductoEnVenta]
    telefono_cliente: Optional[str] = None
    metodo_pago: str
    folio: Optional[str] = None

class VentaChipCreate(BaseModel):
    tipo_chip: str
    numero_telefono: str
    monto_recarga: float
    cvip: bool
    imei: Optional[str] = None
    iccid: Optional[str] = None
    curp: Optional[str] = None
    nip: Optional[str] = None
    fecha_portabilidad: Optional[date] = None
    cambio_chip: bool = False
  

class VentaChipResponse(VentaChipCreate):
    id: int
    empleado_id: Optional[int] = None
    empleado: Optional[UsuarioResponse] = None
    comision: Optional[float] = None
    numero_telefono: str
    fecha: date
    hora: time
    cancelada: bool
    validado: bool
    comision_pagada: bool = False
    descripcion_rechazo: Optional[str] = None
    es_incubadora: bool = False

    class Config:
        from_attributes = True


class PortabilidadItem(BaseModel):
    id: int
    fecha: date
    hora: time
    tipo_chip: str
    numero_telefono: str
    curp: Optional[str] = None
    iccid: Optional[str] = None
    nip: Optional[str] = None
    asesor: Optional[str] = None
    modulo: Optional[str] = None
    portabilidad_realizada: bool
    fecha_portabilidad: Optional[datetime] = None

    class Config:
        from_attributes = True


class MarcarPortabilidadRequest(BaseModel):
    realizada: bool


class PagarComisionesInput(BaseModel):
    chip_ids: list[int]

class DetalleIncubadoraItem(BaseModel):
    numero: str
    empleado: str
    comision: float

class PagarComisionesResponse(BaseModel):
    chips_normales_pagados: int
    chips_incubadora_validados: int
    chips_no_encontrados: int
    total_pagado_normales: float
    total_pendiente_incubadora: float
    detalle_incubadora: list[DetalleIncubadoraItem]


class ComisionCreate(BaseModel):
    producto: str
    cantidad: float

class ComisionUpdate(BaseModel):
    cantidad: float

class ComisionResponse(ComisionCreate):
    id: int

    class Config:
        from_attributes = True




        

class ModuloSelect(BaseModel):
    modulo: str
    
class ModuloResponse(BaseModel):
    id: int
    nombre: str

    class Config:
        from_attributes = True


class CadenaResponse(BaseModel):
    id: int
    codigo: str
    nombre: str
    activo: bool

    class Config:
        from_attributes = True


class TiendaCreate(BaseModel):
    nombre: str
    cadena_id: Optional[int] = None
    num_tienda: Optional[int] = None
    garantizados: Optional[float] = 0
    clave_1: Optional[str] = None
    clave_2: Optional[str] = None
    clave_3: Optional[str] = None
    clave_4: Optional[str] = None


class TiendaUpdate(BaseModel):
    nombre: Optional[str] = None
    activo: Optional[bool] = None
    cadena_id: Optional[int] = None
    num_tienda: Optional[int] = None
    garantizados: Optional[float] = None
    clave_1: Optional[str] = None
    clave_2: Optional[str] = None
    clave_3: Optional[str] = None
    clave_4: Optional[str] = None


class TiendaResponse(BaseModel):
    id: int
    nombre: str
    activo: bool
    cadena_id: Optional[int] = None
    num_tienda: Optional[int] = None
    garantizados: Optional[float] = None
    clave_1: Optional[str] = None
    clave_2: Optional[str] = None
    clave_3: Optional[str] = None
    clave_4: Optional[str] = None
    cadena: Optional[CadenaResponse] = None

    class Config:
        from_attributes = True


class TiendaClaveResponse(BaseModel):
    id: int
    tienda_id: int
    clave: str
    en_uso: bool
    usuario: Optional[str] = None
    password: Optional[str] = None
    notas: Optional[str] = None
    tienda_nombre: Optional[str] = None
    cadena: Optional[str] = None
    num_tienda: Optional[int] = None

    class Config:
        from_attributes = True


class TiendaClaveCreate(BaseModel):
    tienda_id: int
    clave: str
    en_uso: Optional[bool] = False
    usuario: Optional[str] = None
    password: Optional[str] = None
    notas: Optional[str] = None


class TiendaClaveUpdate(BaseModel):
    clave: Optional[str] = None
    en_uso: Optional[bool] = None
    usuario: Optional[str] = None
    password: Optional[str] = None
    notas: Optional[str] = None




class TraspasoBase(BaseModel):
    producto: str
    cantidad: int
    modulo_destino: str
    imei: Optional[str] = None


class TraspasoCreate(TraspasoBase):
    pass

class TraspasoUpdate(BaseModel):
    estado: Literal["aprobado", "rechazado"]


class TraspasoResponse(TraspasoBase):
    id: int
    modulo_origen: str
    estado: str
    fecha: datetime
    solicitado_por: int
    aprobado_por: Optional[int] = None
    visible_en_pendientes: bool 
    clave: Optional[str] = None
    precio: Optional[float] = None
    tipo_producto: Optional[str] = None
    folio: str | None

    class Config:
        from_attributes = True





class InventarioGeneralCreate(BaseModel):
    cantidad: int
    clave: str
    producto: str
    precio: int
    tipo_producto: str


class InventarioGeneralUpdate(BaseModel):
    cantidad: int


class InventarioGeneralPrecioUpdate(BaseModel):
    precio: int


class InventarioGeneralResponse(BaseModel):
    id: int
    producto: str
    clave: str
    cantidad: int
    precio: int
    tipo_producto: str

    class Config:
        from_attributes = True




class InventarioModuloCreate(BaseModel):
    cantidad: int
    clave: str
    producto: str
    precio: int
    modulo_id: int
    tipo_producto: Optional[str] = None


class InventarioModuloUpdate(BaseModel):
    cantidad: Optional[int] = None 
    precio: Optional[int] = None
    modulo_id: Optional[int] = None

class InventarioModuloResponse(BaseModel):
    id: int
    producto: str
    clave : str
    cantidad: int
    precio: int
    modulo: ModuloOut  

    class Config:
        from_attributes = True

class InventarioGlobalCreate(BaseModel):
    cantidad: int
    clave: str
    producto: str
    precio: int
    tipo_producto: str


class InventarioGlobalUpdate(BaseModel):
    cantidad: Optional[int] = None
    precio: Optional[int] = None
    clave: Optional[str] = None
    producto: Optional[str] = None
    tipo_producto: Optional[str] = None



class MovimientoInventarioModulo(BaseModel):
    producto_id: int
    modulo: str
    cantidad: int


class VentaTelefonoCreate(BaseModel):
    marca: str
    modelo: str
    tipo: str
    precio_venta: float
    metodo_pago: str

class VentaTelefonoResponse(BaseModel):
    id: int
    empleado_id: int
    fecha: date
    tipo: str
    hora: time
    cancelada: bool
    empleado: Optional[UsuarioResponse] = None

    class Config:
        from_attributes = True


class InventarioTelefonoGeneralCreate(BaseModel):
    marca: str
    modelo: str
    cantidad: int
    precio: float


class InventarioTelefonoGeneralResponse(BaseModel):
    id: int
    marca: str
    modelo: str
    cantidad: int
    precio: float
    modulo_id: int

    class Config:
        from_attributes = True

class MovimientoTelefonoRequest(BaseModel):
    marca: str
    modelo: str
    cantidad: int
    modulo_id: int


class VentaAccesorioConComision(BaseModel):
    producto: str
    cantidad: int
    comision: float
    tipo_venta: Optional[str] = None
    comision_total: Optional[float] = None
    fecha: Optional[date] = None
    hora: Optional[time] = None
    congelado: bool = False


class VentaTelefonoConComision(BaseModel):
    producto: str
    cantidad: int
    tipo_venta: str
    comision: Optional[float] = None
    comision_total: float
    fecha: Optional[date] = None
    hora: Optional[time] = None
    congelado: bool = False


class VentaChipConComision(BaseModel):
    tipo_chip: str
    numero_telefono: Optional[str] = None
    comision: float
    es_incubadora: Optional[bool] = False
    fecha: Optional[date] = None
    hora: Optional[time] = None
    congelado: bool = False

class ComisionesCicloResponse(BaseModel):
    inicio_ciclo: date
    fin_ciclo: date
    fecha_pago: Optional[date] = None
    total_chips: float
    total_accesorios: float
    total_telefonos: float
    total_general: float
    ventas_accesorios: List[VentaAccesorioConComision]
    ventas_telefonos: List[VentaTelefonoConComision]
    ventas_chips: List[VentaChipConComision]
    dias_congelados: int = 0
    congelado: bool = False

class CorteDiaCreate(BaseModel):
    fecha: date
    # accesorios
    accesorios_efectivo: float
    accesorios_tarjeta: float
    accesorios_total: float
    # teléfonos
    telefonos_efectivo: float
    telefonos_tarjeta: float
    telefonos_total: float
    # totales generales
    total_efectivo: float
    total_tarjeta: float
    total_sistema: float
    total_general: float
    # adicionales
    adicional_recargas: float
    adicional_transporte: float
    adicional_otros: float
    

class RecargasUpdate(BaseModel):
    adicional_recargas: float = 0
    adicional_transporte: float = 0
    adicional_otros: float = 0
    adicional_mayoreo: float = 0
    adicional_mayoreo_para: Optional[str] = None

class SalidaUpdate(BaseModel):
    salida_efectivo: float = 0
    nota_salida: Optional[str] = None

class CorteDiaResponse(BaseModel):
    id: int
    fecha: date
    modulo_id: int
    accesorios_efectivo: float
    accesorios_tarjeta: float
    accesorios_total: float
    telefonos_efectivo: float
    telefonos_tarjeta: float
    telefonos_total: float
    total_efectivo: float
    total_tarjeta: float
    total_sistema: float
    total_general: float
    adicional_recargas: float
    adicional_transporte: float
    adicional_otros: float
    adicional_mayoreo: float
    adicional_mayoreo_para: Optional[str]
    salida_efectivo: float
    nota_salida: Optional[str]
    enviado: bool
    revisado_direccion: bool = False
    revisado_por: Optional[str] = None
    revisado_at: Optional[datetime] = None
    caja_chica: float = 0.0
    devoluciones: Optional[float] = 0.0

    class Config:
        from_attributes = True


class CortePendienteItem(BaseModel):
    id: int
    modulo_id: int
    modulo_nombre: str
    fecha: date
    total_efectivo: float
    total_tarjeta: float
    total_general: float


class CorteRevisarResponse(BaseModel):
    revisado_direccion: bool
    revisado_por: str
    revisado_at: datetime


class RecargaItemResponse(BaseModel):
    id: int
    modulo_id: int
    modulo_nombre: str
    fecha: date
    adicional_recargas: float
    adicional_transporte: float
    adicional_otros: float
    adicional_mayoreo: float
    adicional_mayoreo_para: Optional[str] = None
    recarga_revisada: bool


class EditarRecargasBody(BaseModel):
    adicional_recargas: float = 0
    adicional_transporte: float = 0
    adicional_otros: float = 0
    adicional_mayoreo: float = 0


class ComisionInput(BaseModel):
    comision_manual: Optional[float] = None
    
    
class ValidarChipIncubadoraRequest(BaseModel):
    comision_manual: Optional[float] = None

class InventarioFisicoBase(BaseModel):
    producto: str
    clave: str
    cantidad: int

class InventarioFisicoCreate(InventarioFisicoBase):
    pass

class InventarioFisicoResponse(InventarioFisicoBase):
    id: int
    fecha: datetime

    class Config:
        orm_mode = True



class ItemConteo(BaseModel):
    producto_id: int
    cantidad: int

class ConteoRequest(BaseModel):
    modulo_id: int
    productos: List[ItemConteo]


class ProductoConteo(BaseModel):
    clave: str
    cantidad: int

class ConteoInventarioRequest(BaseModel):
    modulo_id: int
    productos: List[ProductoConteo]


class EntradaItem(BaseModel):
    producto_id: int
    cantidad: int

class EntradaMercanciaRequest(BaseModel):
    modulo_id: int
    productos: list[EntradaItem]


class EditarEntradaRequest(BaseModel):
    productos: list[EntradaItem]


class EditarPrecioVentaRequest(BaseModel):
    nuevo_precio: float


class EncargadoResponse(BaseModel):
    modulo_id: int
    usuario_id: int
    username: str
    nombre_completo: Optional[str] = None

    class Config:
        from_attributes = True


class ProductoEntradaDetalle(BaseModel):
    clave: str
    producto: str
    cantidad: int


class EntradaMercanciaListItem(BaseModel):
    id: int
    folio: str
    fecha: datetime
    modulo_id: int
    modulo_nombre: str
    usuario_id: int
    usuario_username: str
    usuario_nombre: str
    productos: List[ProductoEntradaDetalle]




class NominaPeriodoResponse(BaseModel):
    id: int
    fecha_inicio: date
    fecha_fin: date
    estado: str
    

    class Config:
        from_attributes = True

class NominaPeriodoCreate(BaseModel):
    fecha_inicio: date
    fecha_fin: date
    


class NominaEmpleadoResponse(BaseModel):
    usuario_id: int
    username: str
    comisiones: float
    comisiones_accesorios: float = 0
    comisiones_telefonos: float = 0
    comisiones_chips: float = 0
    sueldo_base: float
    horas_extra: int
    pago_hora_extra: float
    precio_hora_extra: float
    sanciones: float
    comisiones_pendientes: float
    total_pagar: float
    total_comisiones: float



class NominaEmpleadoUpdate(BaseModel):

    horas_extra: Optional[int] = None
    precio_hora_extra: Optional[float] = None
    sanciones: Optional[float] = None
    comisiones_pendientes: Optional[float] = None


class NominaPeriodoFechasUpdate(BaseModel):
    inicio_a: Optional[date] = None
    fin_a: Optional[date] = None
    inicio_c: Optional[date] = None
    fin_c: Optional[date] = None

class PlanCreate(BaseModel):
    tipo_tramite: str
    tipo_plan: str
    empleado_id: int
    modulo_id: int


class NominaHistorialEmpleado(BaseModel):
    usuario_id: int
    username: str
    grupo: str
    comisiones_accesorios: float = 0
    comisiones_telefonos: float = 0
    comisiones_chips: float = 0
    comisiones_total: float = 0
    sueldo_base: float = 0
    horas_extra: float = 0
    precio_hora_extra: float = 0
    pago_horas_extra: float = 0
    sanciones: float = 0
    comisiones_pendientes: float = 0
    horas_faltantes: float = 0
    total_pagar: float = 0


class NominaHistorialCreate(BaseModel):
    semana_inicio: date
    semana_fin: date
    comisiones_inicio_a: date
    comisiones_fin_a: date
    comisiones_inicio_c: date
    comisiones_fin_c: date
    empleados: list[NominaHistorialEmpleado]


class NominaHistorialResponse(BaseModel):
    id: int
    semana_inicio: date
    semana_fin: date
    comisiones_inicio: date
    comisiones_fin: date
    usuario_id: int
    username: str
    grupo: str
    comisiones_accesorios: float
    comisiones_telefonos: float
    comisiones_chips: float
    comisiones_total: float
    sueldo_base: float
    horas_extra: float
    precio_hora_extra: float
    pago_horas_extra: float
    sanciones: float
    comisiones_pendientes: float
    horas_faltantes: float = 0
    total_pagar: float
    guardado_at: datetime

    class Config:
        from_attributes = True


class VentaResumenItem(BaseModel):
    id: int
    producto: str
    tipo_producto: str
    tipo_venta: Optional[str] = None
    precio_unitario: float
    cantidad: int
    total: Optional[float] = None
    metodo_pago: Optional[str] = None
    empleado_username: Optional[str] = None
    cancelada: bool = False


class DireccionCorteResponse(CorteDiaResponse):
    chips_count: int = 0
    chips_por_tipo: Dict[str, int] = {}
    ventas: List[VentaResumenItem] = []


class ModuloStockItem(BaseModel):
    modulo: str
    cantidad: int


class ProductoBusquedaResult(BaseModel):
    producto: str
    total: int
    modulos: List[ModuloStockItem]


class StockPorModuloItem(BaseModel):
    modulo: str
    total_productos: int
    tipos_distintos: int


# ── Estadísticas del mes ──────────────────────────────────────────────────────

class CantidadMonto(BaseModel):
    cantidad: int
    monto: float

class TipoChipStatItem(BaseModel):
    tipo_chip: str
    cantidad: int

class MontoRecargaStatItem(BaseModel):
    monto: str
    cantidad: int

class TopProductoItem(BaseModel):
    producto: str
    cantidad: int
    monto: float

class TramiteStatItem(BaseModel):
    tramite: str
    cantidad: int

class PlanStatItem(BaseModel):
    plan: str
    cantidad: int

class ModuloEstadItem(BaseModel):
    modulo: str
    total_mxn: float
    telefonos_contado: int
    telefonos_payjoy: int
    telefonos_paguitos: int
    telefonos_plan: int = 0
    telefonos_total: int
    chips: int
    accesorios: int
    planes: int
    promedio_historico: float
    meta_proporcional: float
    productividad_pct: Optional[float] = None
    meses_considerados: int = 0

class VentaDiaItem(BaseModel):
    dia: int
    total: float

class ResumenGeneralStats(BaseModel):
    total_ventas_mxn: float
    total_telefonos: int
    total_chips: int
    total_accesorios: int
    total_planes: int

class TelefonosStats(BaseModel):
    total: int
    contado: CantidadMonto
    payjoy: CantidadMonto
    paguitos: CantidadMonto
    plan: CantidadMonto
    sin_clasificar: CantidadMonto

class AccesoriosStats(BaseModel):
    total_unidades: int
    monto_total: float
    top_5_productos: List[TopProductoItem]
    top_10_productos: List[dict] = []

class ChipsStats(BaseModel):
    total: int
    por_tipo: List[TipoChipStatItem]
    por_monto_recarga: List[MontoRecargaStatItem]

class PlanesStats(BaseModel):
    total: int
    por_tramite: List[TramiteStatItem]
    por_plan: List[PlanStatItem]
    contratos_pendientes: int = 0
    contratos_listos: int = 0

class TelefonoModuloItem(BaseModel):
    modulo: str
    total_telefonos: int
    monto_total: float
    contado: int
    payjoy: int
    paguitos: int
    plan: int = 0

class EstadisticasMesResponse(BaseModel):
    mes: str
    periodo_texto: str
    resumen_general: ResumenGeneralStats
    telefonos: TelefonosStats
    accesorios: AccesoriosStats
    chips: ChipsStats
    planes: PlanesStats
    por_modulo: List[ModuloEstadItem]
    ventas_por_dia: List[VentaDiaItem]
    telefonos_por_modulo: List[TelefonoModuloItem]
    telefonos_por_dia: List[dict] = []
    telefonos_top: List[dict] = []
    accesorios_por_dia: List[dict] = []


# ── Tiempo Real ───────────────────────────────────────────────────────────────

class TiempoRealResumen(BaseModel):
    total_ventas_mxn: float
    total_telefonos: int
    total_chips: int
    total_accesorios: int

class TelefonoHoyItem(BaseModel):
    hora: str
    modulo: str
    asesor: str
    producto: str
    tipo_venta: str
    precio: float

class ModuloTiempoRealItem(BaseModel):
    modulo: str
    total_mxn: float
    telefonos_contado: int
    telefonos_payjoy: int
    telefonos_paguitos: int
    telefonos_plan: int = 0
    telefonos_total: int
    chips: int
    accesorios: int
    promedio_diario_historico: float
    meta_proporcional: float
    productividad_pct: Optional[float] = None
    dias_considerados: int = 0

class TiempoRealResponse(BaseModel):
    fecha: str
    fecha_texto: str
    hora_actual: str
    horas_transcurridas: float
    horas_totales: int
    porcentaje_dia: float
    resumen_general: TiempoRealResumen
    telefonos: TelefonosStats
    chips: ChipsStats
    accesorios: AccesoriosStats
    lista_telefonos_hoy: List[TelefonoHoyItem]
    por_modulo: List[ModuloTiempoRealItem]
    total_planes_hoy: int = 0
    ultimas_ventas: List[dict] = []


# ── Asistencia semanal / jornada ──────────────────────────────────────────────

class CicloSemana(BaseModel):
    inicio: date
    fin: date
    label: str


class DiaResumen(BaseModel):
    entrada: Optional[datetime] = None
    salida: Optional[datetime] = None
    horas: float = 0.0


class EmpleadoAcumuladoSemanal(BaseModel):
    usuario_id: int
    username: str
    nombre_completo: Optional[str] = None
    nombre_englobado: Optional[str] = None
    dias: Dict[str, Optional[DiaResumen]]
    total_horas: float
    jornada: Optional[float] = None
    horas_extra: Optional[float] = None
    jornada_fija: Optional[float] = 0
    sueldo_base: Optional[float] = 0
    descansos: List[str] = []


class JornadaUpsert(BaseModel):
    usuario_id: int
    ciclo: date
    horas: float


# ── Sueldos encargados ────────────────────────────────────────────────────────

class ProductoResumen(BaseModel):
    nombre: str
    tipo: str  # "telefono" | "accesorio"
    cantidad: int
    neto: float
    porcentaje_label: str  # "$10 fijo" | "7.00%"
    comision: float
    congelado: bool = False


class DiaDiario(BaseModel):
    fecha: Optional[date] = None  # None en la fila TOTAL
    label: str
    equipos: int
    accesorios: float
    congelado: bool = False


class SueldoEncargadoResponse(BaseModel):
    modulo: str
    fecha_inicio: date
    fecha_fin: date
    porcentaje_modulo: float
    productos: List[ProductoResumen]
    desglose_diario: List[DiaDiario]
    sueldo_total: float
    dias_congelados: int = 0
    congelado: bool = False


class ResumenEmpleadoNomina(BaseModel):
    nombre: str
    rol: str
    sueldo_base: float
    horas_extras_pagadas: float
    comisiones: float
    total: float


class ResumenModuloResponse(BaseModel):
    nomina_inicio: date
    nomina_fin: date
    empleados: List[ResumenEmpleadoNomina]


class CajaChicaCreate(BaseModel):
    modulo_id: int
    fecha: date
    monto: float


class CajaChicaResponse(BaseModel):
    id: int
    modulo_id: int
    fecha: date
    monto: float

    class Config:
        from_attributes = True


class CicloGuardadoCreate(BaseModel):
    concepto: str
    etiqueta: str
    fecha_inicio: date
    fecha_fin: date
    datos: List[Dict]


class CicloGuardadoResponse(BaseModel):
    id: int
    concepto: str
    etiqueta: str
    fecha_inicio: date
    fecha_fin: date
    datos: List[Dict]
    creado_por: str
    creado_en: datetime

    class Config:
        from_attributes = True


class NominaCreate(BaseModel):
    etiqueta: str
    ciclo_horas_extras_id: Optional[int] = None
    chip_ids_incubadora: Optional[List[int]] = None
    fecha_inicio_asesores: Optional[date] = None
    fecha_fin_asesores: Optional[date] = None
    fecha_inicio_encargados: Optional[date] = None
    fecha_fin_encargados: Optional[date] = None
    fecha_inicio_cadenas: Optional[date] = None
    fecha_fin_cadenas: Optional[date] = None
    datos: List[Dict]


class NominaListItem(BaseModel):
    id: int
    etiqueta: str
    total_pago: float
    publicada: bool = False
    creado_en: datetime

    class Config:
        from_attributes = True


class NominaResponse(BaseModel):
    id: int
    etiqueta: str
    ciclo_horas_extras_id: Optional[int] = None
    ciclo_comisiones_id: Optional[int] = None
    fecha_inicio_asesores: Optional[date] = None
    fecha_fin_asesores: Optional[date] = None
    fecha_inicio_encargados: Optional[date] = None
    fecha_fin_encargados: Optional[date] = None
    fecha_inicio_cadenas: Optional[date] = None
    fecha_fin_cadenas: Optional[date] = None
    total_pago: float
    datos: List[Dict]
    publicada: bool = False
    creado_por: str
    creado_en: datetime

    class Config:
        from_attributes = True


class NominaIncubadoraCreate(BaseModel):
    etiqueta: str
    datos: List[Dict]


class NominaIncubadoraListItem(BaseModel):
    id: int
    etiqueta: str
    total_pago: float
    creado_en: datetime

    class Config:
        from_attributes = True


class NominaIncubadoraResponse(BaseModel):
    id: int
    etiqueta: str
    total_pago: float
    datos: List[Dict]
    creado_por: str
    creado_en: datetime

    class Config:
        from_attributes = True


# ── Ajustes de Inventario ────────────────────────────────────────────────────

class AjusteItemCreate(BaseModel):
    clave: str
    cantidad_nueva: int


class AjusteInventarioCreate(BaseModel):
    modulo_id: int
    motivo: Optional[str] = None
    productos: List[AjusteItemCreate]


class AjusteItemResponse(BaseModel):
    id: int
    clave: str
    producto: str
    cantidad_anterior: int
    cantidad_nueva: int
    delta: int

    class Config:
        from_attributes = True


class AjusteInventarioResponse(BaseModel):
    id: int
    folio: str
    modulo_id: int
    modulo_nombre: str
    usuario_id: int
    usuario_nombre: str
    fecha: datetime
    motivo: Optional[str] = None
    items: List[AjusteItemResponse]

    class Config:
        from_attributes = True


# ── Conteos Físicos ───────────────────────────────────────────────────────────

class ItemPreviewActualizar(BaseModel):
    clave: str
    producto: str
    cantidad_actual: int
    cantidad_nueva: int
    diferencia: int

class ItemPreviewCrear(BaseModel):
    clave: str
    producto: str
    cantidad: int

class ItemPreviewDecision(BaseModel):
    clave: str
    producto: str
    cantidad_actual: int

class ErrorItemConteo(BaseModel):
    fila: int
    clave: str
    motivo: str

class ProcesamientoResponse(BaseModel):
    modulo_id: int
    modulo_nombre: str
    total_filas_excel: int
    advertencia_volumen: bool
    para_actualizar: List[ItemPreviewActualizar]
    para_crear: List[ItemPreviewCrear]
    decidir_caso_por_caso: List[ItemPreviewDecision]
    errores: List[ErrorItemConteo]

class ItemAplicarActualizar(BaseModel):
    clave: str
    producto: str
    cantidad_nueva: int

class ItemAplicarCrear(BaseModel):
    clave: str
    producto: str
    cantidad: int

class ItemAplicarDecision(BaseModel):
    clave: str
    producto: str
    poner_en_cero: bool = False

class ItemAplicarImei(BaseModel):
    imei: str
    clave: str | None = None
    producto: str | None = None
    resultado: str          # ok | reasignado | vendido_presente | pendiente_alta
    equipo_id: int | None = None
    estatus_sistema: str | None = None
    modulo_sistema_id: int | None = None

class FaltanteImeiItem(BaseModel):
    imei: str
    clave: str
    producto: str
    fecha_salida: str | None = None
    dias_en_piso: int | None = None

class AplicarRequest(BaseModel):
    modulo_id: int
    archivo_nombre: str
    total_filas_excel: int
    para_actualizar: List[ItemAplicarActualizar]
    para_crear: List[ItemAplicarCrear]
    caso_por_caso: List[ItemAplicarDecision]
    notas: Optional[str] = None
    imeis: List[ItemAplicarImei] = []
    # Solo si es True se manda a cero el telefono que no se pistoleo (bloque C3
    # de /aplicar). False = conteo parcial: lo no escaneado se queda como esta.
    conteo_imei_completo: bool = False
    # Segunda confirmacion explicita del admin para dar de baja los IMEIs
    # faltantes. Solo surte efecto si conteo_imei_completo tambien es True.
    # False = flujo actual: los faltantes solo se reportan, no se tocan.
    dar_de_baja_faltantes: bool = False

class AplicarResponse(BaseModel):
    folio: str
    modulo: str
    productos_actualizados: int
    productos_creados: int
    productos_en_cero: int
    productos_conservados: int
    faltantes_imei: List[FaltanteImeiItem] = []
    imeis_registrados: int = 0
    faltantes_dados_de_baja: int = 0

class ConteoFisicoItemResponse(BaseModel):
    id: int
    clave: Optional[str] = None
    producto: Optional[str] = None
    cantidad_anterior: Optional[int] = None
    cantidad_nueva: Optional[int] = None
    accion: Optional[str] = None
    producto_creado: bool = False
    imeis: List[str] = []
    imeis_escaneados: int = 0
    imei_aplica: bool = False
    imei_check: Optional[str] = None
    imeis_faltantes: List[str] = []

    class Config:
        from_attributes = True

class ConteoFisicoListItem(BaseModel):
    id: int
    folio: str
    modulo: str
    fecha: datetime
    usuario: Optional[str] = None
    archivo_nombre: Optional[str] = None
    total_filas: Optional[int] = None
    productos_actualizados: Optional[int] = None
    productos_creados: Optional[int] = None
    productos_en_cero: Optional[int] = None
    estado: str
    notas: Optional[str] = None

    class Config:
        from_attributes = True

class ConteoFisicoDetalleResponse(ConteoFisicoListItem):
    items: List[ConteoFisicoItemResponse]
    imeis_sin_clave: List[str] = []
    total_imeis: int = 0

class RevertirResponse(BaseModel):
    folio: str
    estado: str
    items_revertidos: int
    advertencias: List[str]

class ValidarImeiRequest(BaseModel):
    modulo_id: int
    imei: str

class ValidarImeiResponse(BaseModel):
    imei: str
    encontrado: bool
    resultado: str
    clave: str | None = None
    producto: str | None = None
    estatus_sistema: str | None = None
    modulo_sistema_id: int | None = None
    modulo_sistema_nombre: str | None = None
    mensaje: str

class KardexLineaItem(BaseModel):
    fecha: datetime
    tipo: str
    entrada: int
    salida: int
    existencia: int

class ConteoAnteriorInfo(BaseModel):
    folio: str
    fecha: datetime
    saldo_inicial: int

class KardexProductoResponse(BaseModel):
    clave: str
    producto: str
    modulo: str
    tiene_comparativo: bool = True
    conteo_anterior: Optional[ConteoAnteriorInfo] = None
    movimientos: List[KardexLineaItem]
    total_entradas: int
    total_salidas: int
    saldo_calculado: Optional[int] = None
    contado: int
    diferencia: Optional[int] = None


class PlanTarifarioCreate(BaseModel):
    tipo_plan: Optional[str] = None
    estatus: Optional[str] = None
    categoria: Optional[str] = None
    clasificacion: Optional[str] = None
    equipo: Optional[str] = None
    imei: Optional[str] = None
    precio_equipo: Optional[float] = None
    plazo: Optional[int] = None
    linea: Optional[str] = None
    cuenta: Optional[str] = None
    pago_inicial: Optional[bool] = False
    monto_pago_inicial: Optional[float] = 0
    metodo_pago_inicial: Optional[str] = None
    monto_inicial_efectivo: Optional[float] = 0
    monto_inicial_tarjeta: Optional[float] = 0


class PlanTarifarioUpdate(BaseModel):
    fecha: Optional[datetime] = None
    tipo_plan: Optional[str] = None
    estatus: Optional[str] = None
    categoria: Optional[str] = None
    clasificacion: Optional[str] = None
    imei: Optional[str] = None
    precio_equipo: Optional[float] = None
    plazo: Optional[int] = None
    linea: Optional[str] = None
    cuenta: Optional[str] = None
    pago_inicial: Optional[bool] = None
    metodo_pago_inicial: Optional[str] = None
    pagado: Optional[bool] = None
    contrato_listo: Optional[bool] = None


class PlanTarifarioResponse(BaseModel):
    id: int
    fecha: Optional[datetime] = None
    empleado_id: Optional[int] = None
    modulo_id: Optional[int] = None
    tipo_plan: Optional[str] = None
    estatus: Optional[str] = None
    categoria: Optional[str] = None
    clasificacion: Optional[str] = None
    equipo: Optional[str] = None
    imei: Optional[str] = None
    precio_equipo: Optional[float] = None
    plazo: Optional[int] = None
    linea: Optional[str] = None
    cuenta: Optional[str] = None
    pago_inicial: Optional[bool] = None
    monto_pago_inicial: Optional[float] = None
    metodo_pago_inicial: Optional[str] = None
    pagado: Optional[bool] = None
    fecha_pago: Optional[datetime] = None
    contrato_listo: Optional[bool] = None
    venta_pi_id: Optional[int] = None

    class Config:
        from_attributes = True


# ── Schemas de Gastos ─────────────────────────────────────────────────────────

class GastoCreate(BaseModel):
    tipo: Literal["personal", "iglesia", "prestamos"]
    concepto: str
    monto: float


class GastoUpdate(BaseModel):
    tipo: Literal["personal", "iglesia", "prestamos"]
    concepto: str
    monto: float


class GastoAbonoCreate(BaseModel):
    monto: float
    nota: Optional[str] = None


class GastoAbonoResponse(BaseModel):
    id: int
    monto: float
    fecha: Optional[datetime] = None
    nota: Optional[str] = None

    class Config:
        from_attributes = True


class GastoResponse(BaseModel):
    id: int
    tipo: str
    concepto: str
    monto: float
    estado: str
    fecha_registro: Optional[datetime] = None
    fecha_pago: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── Mi semana (detalle lunes→domingo) ────────────────────────────────────────
class MarcaDia(BaseModel):
    hora: str
    foto_url: Optional[str] = None
    dentro_de_zona: Optional[bool] = None


class DiaSemanaDetalle(BaseModel):
    fecha: date
    dia_semana: str
    entrada: Optional[MarcaDia] = None
    salida: Optional[MarcaDia] = None
    horas: Optional[float] = None
    estado: str


class SemanaDetalle(BaseModel):
    semana_inicio: date
    semana_fin: date
    dias: List[DiaSemanaDetalle]


class DevolucionCreate(BaseModel):
    monto: float
    motivo: Optional[str] = None


# TiendaResponse se define despues de UsuarioResponse: resuelve la forward ref.
UsuarioResponse.model_rebuild()


class CapturaTelcelResponse(BaseModel):
    id: int
    username: str
    fecha: date
    tipo: str
    clave: str
    hora_entrada: Optional[time] = None
    hora_salida: Optional[time] = None
    duracion_minutos: Optional[int] = None
    foto_url: str

    class Config:
        from_attributes = True


class CapturaTelcelCreate(BaseModel):
    tipo: str
    foto_base64: str
    usuario_id: Optional[int] = None
