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
    return [TIPOS[cfg["tipo"]](clave, cfg)
            for clave, cfg in config_tiendas.items() if cfg.get("activa", True)]
