"""Lector de listados con datos embebidos en la página (__NEXT_DATA__) y
filtro de descuento propio de la tienda. Direcciones, parámetros y categorías
vienen de la configuración privada."""

from ofertas.modelos import Fuente, Producto
from ofertas.tiendas.base import (ErrorFormato, Tienda, etiquetas_utiles, limpiar, next_data,
                                   numero, slug)


class ListadoFiltrado(Tienda):

    def fuentes(self, cursor):
        fuentes = []
        for entrada in self.cfg["categorias"]:
            # "categoria@80" = esa categoría pidiendo al menos 80 % de descuento.
            cat, _, minimo = entrada.partition("@")
            minimo = int(minimo or self.cfg.get("min_descuento_filtro", 70))
            # El id cambia si cambia la cobertura, para recalcular la línea base.
            fuentes.append(Fuente(id=f"{self.clave}:{cat}:{minimo}:p{self.cfg.get('max_paginas', 3)}",
                                  url=self.cfg["url_listado"].format(categoria=cat, minimo=minimo)))
        return fuentes, cursor

    def leer(self, fuente, d):
        # De mayor a menor precio: las ofertas de más valor salen primero.
        productos, leidos, total = [], 0, 0
        for pagina in range(1, int(self.cfg.get("max_paginas", 3)) + 1):
            if pagina > 1 and d.sin_tiempo():
                break
            data = self._listado(d, fuente.url + self.cfg["orden_mayor"].format(pagina=pagina))
            lote = (data or {}).get("products") or []
            productos.extend(p for p in map(self._producto, lote) if p)
            total = int((data or {}).get("total") or 0)
            leidos = pagina * int((data or {}).get("limit") or 48)
            if not lote or leidos >= total:
                break
        if total > leidos and not d.sin_tiempo():
            # Quedaron páginas sin leer: se añaden los precios más bajos, que es
            # donde aparecen los errores tipo "S/ 9".
            data = self._listado(d, fuente.url + self.cfg["orden_menor"].format(pagina=1))
            productos.extend(p for p in map(self._producto, (data or {}).get("products") or []) if p)
        return productos

    def _listado(self, d, url):
        datos = next_data(d.texto(url))
        if "props" not in datos:
            raise ErrorFormato("respuesta sin listado de productos")
        fp = ((datos["props"] or {}).get("pageProps") or {}).get("findabilityProps") or {}
        if fp.get("error") and fp.get("error") == self.cfg.get("error_sin_resultados"):
            return None  # ningún producto cumple el filtro de descuento ahora
        if fp.get("error"):
            raise ErrorFormato(f"la tienda respondió {fp['error']}")
        data = fp.get("data")
        if data and self.cfg.get("parametro_filtro") \
                and self.cfg["parametro_filtro"] not in (fp.get("queryParams") or {}):
            # Pasa si la tienda redirige la categoría: el listado llega SIN filtro.
            raise ErrorFormato("la categoría redirige y pierde el filtro de descuento; "
                               "usa en la configuración la dirección final de la categoría")
        # None si la categoría existe pero no es un listado (p. ej. una portada).
        return data

    def _producto(self, p):
        precio = numero(p.get("priceNumber"))
        nombre = limpiar(p.get("name"))
        if not precio or not nombre:
            return None
        antes = numero(p.get("masterPriceNumber"))
        tarjeta = numero(p.get(self.cfg.get("campo_precio_tarjeta", "")))
        padre = str(p.get("parentProductID") or p.get("sku")).lower()
        vendedor = (p.get("shop") or {}).get("shopName") or p.get("seller")
        badges = (p.get("badgesTop") or []) + (p.get("badgesMiddle") or [])
        propio = str(self.cfg.get("vendedor_propio", "")).upper()
        return Producto(
            tienda=self.clave,
            id=str(p.get("sku")),
            nombre=nombre,
            url=self.cfg["url_producto"].format(slug=slug(nombre), id=padre),
            precio=precio,
            precio_antes=antes if antes and antes > precio else None,
            precio_tarjeta=tarjeta if tarjeta and tarjeta < precio else None,
            imagen=p.get("primaryImage"),
            disponible=p.get("inStock"),
            vendedor=vendedor,
            marketplace=str(p.get("seller", "")).upper() != propio,
            etiquetas=etiquetas_utiles(badges),
        )
