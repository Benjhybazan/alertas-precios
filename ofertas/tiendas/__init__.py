from ofertas.tiendas.listado_categoria import ListadoCategoria
from ofertas.tiendas.listado_filtrado import ListadoFiltrado
from ofertas.tiendas.magento import MagentoGraphql, MagentoListado
from ofertas.tiendas.vtex import Vtex

# tipo (en la configuración privada) -> clase que sabe leer esa tienda
TIPOS = {
    "listado_filtrado": ListadoFiltrado,
    "listado_categoria": ListadoCategoria,
    "vtex": Vtex,
    "magento_graphql": MagentoGraphql,
    "magento_listado": MagentoListado,
}


def crear_tiendas(config_tiendas: dict):
    """Devuelve (tiendas, invalidas). Una tienda mal configurada (tipo
    desconocido, expresión inválida...) se omite sin afectar a las demás."""
    tiendas, invalidas = [], []
    for clave, cfg in (config_tiendas or {}).items():
        if not isinstance(cfg, dict):
            invalidas.append((clave, "su configuración no es un objeto"))
            continue
        if not cfg.get("activa", True):
            continue
        clase = TIPOS.get(cfg.get("tipo"))
        if clase is None:
            invalidas.append((clave, f"tipo desconocido: {cfg.get('tipo')!r}"))
            continue
        try:
            tiendas.append(clase(clave, cfg))
        except Exception as e:  # p. ej. una expresión regular inválida
            invalidas.append((clave, f"{e.__class__.__name__}: {e}"))
    return tiendas, invalidas
