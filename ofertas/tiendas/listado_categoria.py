"""Lector de listados de categoría con datos embebidos en la página
(__NEXT_DATA__) para tiendas sin filtro de descuento. Combina listados
ordenados por precio (con "pisos" de precio por categoría) y páginas de
categorías amplias. Direcciones, categorías, pisos y exclusiones vienen de la
configuración privada."""

import re

from ofertas.modelos import Fuente, Producto
from ofertas.tiendas.base import Tienda, etiquetas_utiles, limpiar, next_data, numero


class ListadoCategoria(Tienda):

    def __init__(self, clave, cfg):
        super().__init__(clave, cfg)
        excluir = cfg.get("excluir")
        # Accesorios y versiones "mini" que no deben compararse con el piso.
        self._excluir = re.compile(excluir, re.I) if excluir else None
        self._propios = {v.lower() for v in cfg.get("vendedores_propios", [])}

    def fuentes(self, cursor):
        fuentes = [
            Fuente(id=f"{self.clave}:asc:{c['id']}",
                   url=self.cfg["url_precio_bajo"].format(id=c["id"]),
                   datos={"piso": c.get("piso"), "patron": c.get("patron")})
            for c in self.cfg.get("categorias_precio_bajo", [])
        ]
        paginas = int(self.cfg.get("paginas_por_categoria", 3))
        fuentes += [Fuente(id=f"{self.clave}:{cat}:p{pag}",
                           url=self.cfg["url_amplia"].format(id=cat, pagina=pag))
                    for cat in self.cfg.get("categorias_amplias", [])
                    for pag in range(1, paginas + 1)]
        return fuentes, cursor

    def leer(self, fuente, d):
        datos = next_data(d.texto(fuente.url))
        resultados = ((datos.get("props") or {}).get("pageProps") or {}).get("results") or []
        patron = fuente.datos.get("patron")
        patron = re.compile(patron, re.I) if patron else None
        productos = []
        for r in resultados:
            p = self._producto(r)
            if not p:
                continue
            if patron and fuente.datos.get("piso") and patron.search(p.nombre) \
                    and not (self._excluir and self._excluir.search(p.nombre)):
                p.piso = float(fuente.datos["piso"])
            productos.append(p)
        return productos

    def _producto(self, r):
        publicos, tarjeta, antes = [], None, None
        for pr in r.get("prices") or []:
            crudos = pr.get("price") or []
            if not isinstance(crudos, list):
                crudos = [crudos]
            valores = [v for v in map(numero, crudos) if v]
            if not valores:
                continue
            tipo = pr.get("type", "")
            if pr.get("crossed") or tipo == "normalPrice":
                antes = min(valores)  # rangos: mínimo vs. mínimo, sin exagerar
            elif tipo == "cmrPrice":
                tarjeta = min(valores)
            else:  # precio de internet, de evento...
                publicos.append(min(valores))
        if not publicos:
            # Solo hay precio normal (sin oferta): ese es el precio actual.
            if antes is None:
                return None
            publicos, antes = [antes], None
        precio = min(publicos)
        nombre = limpiar(r.get("displayName"))
        if not nombre or not r.get("url"):
            return None
        vendedor = r.get("sellerName") or ""
        badges = (r.get("badges") or []) + (r.get("multipurposeBadges") or [])
        return Producto(
            tienda=self.clave,
            id=str(r.get("skuId") or r.get("productId")),
            nombre=nombre,
            url=r["url"],
            precio=precio,
            precio_antes=antes if antes and antes > precio else None,
            precio_tarjeta=tarjeta if tarjeta and tarjeta < precio else None,
            imagen=(r.get("mediaUrls") or [None])[0],
            vendedor=vendedor or None,
            marketplace=vendedor.strip().lower() not in self._propios,
            etiquetas=etiquetas_utiles(badges),
        )
