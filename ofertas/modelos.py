from dataclasses import dataclass, field


@dataclass
class Producto:
    """Un producto tal como lo publica la tienda. Nunca se inventan datos:
    si la tienda no informa algo (stock, precio con tarjeta...) queda en None."""

    tienda: str                     # clave de la tienda en la configuración
    id: str                         # identificador del producto en la tienda
    nombre: str
    url: str
    precio: float                   # precio público actual (sin tarjeta)
    precio_antes: float | None = None    # precio tachado / de lista
    precio_tarjeta: float | None = None  # precio con tarjeta de la tienda
    imagen: str | None = None
    stock: int | None = None        # unidades, si la tienda lo informa
    disponible: bool | None = None
    vendedor: str | None = None
    marketplace: bool = False       # vendido por un tercero
    etiquetas: list[str] = field(default_factory=list)
    # Precio por debajo del cual el producto se considera anormalmente bajo
    # (solo para fuentes con "piso" configurado y nombre que coincide).
    piso: float | None = None

    @property
    def clave(self) -> str:
        return f"{self.tienda}:{self.id}"


@dataclass
class Fuente:
    """Una página/consulta de una tienda que se descarga en una ejecución."""

    id: str              # identificador estable, usado para la línea base
    url: str
    datos: dict = field(default_factory=dict)  # info extra para el lector
