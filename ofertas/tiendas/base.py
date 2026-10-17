import html
import json
import re
import unicodedata

from ofertas.modelos import Fuente, Producto
from ofertas.red import Descargador


class ErrorFormato(Exception):
    """La página no tiene el formato esperado (cambio en la web o bloqueo)."""


class Tienda:
    """Interfaz común. Para agregar una tienda nueva:
    1. crea una subclase con fuentes() y leer();
    2. regístrala en ofertas/tiendas/__init__.py;
    3. añade su bloque en la configuración privada ("tiendas")."""

    def __init__(self, clave: str, cfg: dict):
        self.clave = clave
        self.cfg = cfg
        self.nombre = cfg.get("nombre", clave)
        # Usar el programa curl en vez del cliente de Python (algunas tiendas
        # solo aceptan curl) y cuántas peticiones simultáneas hacer.
        self.usar_curl = bool(cfg.get("usar_curl", False))
        self.conexiones = int(cfg.get("conexiones", 1))

    def fuentes(self, cursor: int) -> tuple[list[Fuente], int]:
        """Fuentes a revisar en esta ejecución y el nuevo cursor (por si la tienda
        necesita repartir su lectura entre ejecuciones; si no, se devuelve igual)."""
        raise NotImplementedError

    def leer(self, fuente: Fuente, d: Descargador) -> list[Producto]:
        raise NotImplementedError


_RE_NEXT = re.compile(r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S)


def next_data(html: str) -> dict:
    m = _RE_NEXT.search(html)
    if not m:
        raise ErrorFormato("la página no trae datos de productos (__NEXT_DATA__)")
    return json.loads(m.group(1))


def slug(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", texto.lower()).strip("-")


def limpiar(texto) -> str:
    """Quita entidades HTML (&#x27;) y espacios repetidos de un nombre."""
    return re.sub(r"\s+", " ", html.unescape(str(texto or ""))).strip()


def numero(valor) -> float | None:
    """Convierte '7,499' o '209.90' o 3799 a float (formato peruano)."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    limpio = re.sub(r"[^\d.,]", "", str(valor)).replace(",", "")
    try:
        return float(limpio) if limpio else None
    except ValueError:
        return None


_RE_ETIQUETA_UTIL = re.compile(r"rel[aá]mpago|flash|[uú]ltim|liquidaci|remate|cyber|black|outlet", re.I)


def etiquetas_utiles(badges) -> list[str]:
    """Etiquetas de la tienda que indican urgencia (relámpago, últimas unidades...)."""
    vistos = []
    for b in badges:
        t = b.get("label") if isinstance(b, dict) else b
        if isinstance(t, str) and _RE_ETIQUETA_UTIL.search(t) and t.strip() not in vistos:
            vistos.append(t.strip())
    return vistos[:3]
