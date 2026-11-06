"""Lectores para tiendas sobre la plataforma Magento. Direcciones, categorías y
profundidad vienen de la configuración privada.

  * MagentoGraphql: tiendas con la consulta pública de catálogo (GraphQL);
    se lee el catálogo completo en páginas grandes.
  * MagentoListado: tiendas sin consulta pública ni orden por descuento (su
    robots.txt no permite ordenar listados). Cada revisión lee las páginas
    "fijas" (p. ej. la de campaña) y un tramo de las páginas de categoría,
    rotando: en varias revisiones se recorre todo."""

import html
import json
import re
from urllib.parse import quote

from ofertas.modelos import Fuente, Producto
from ofertas.tiendas.base import ErrorFormato, Tienda, limpiar

CONSULTA = ("{products(search:\"\",pageSize:%d,currentPage:%d,sort:{name:ASC}){items{"
            "id name url_key url_suffix stock_status small_image{url} "
            "price_range{minimum_price{regular_price{value} final_price{value}}}}}}")


class MagentoGraphql(Tienda):

    def fuentes(self, cursor):
        lote, paginas = int(self.cfg.get("lote", 200)), int(self.cfg.get("paginas", 30))
        return [Fuente(id=f"{self.clave}:{n}",
                       url=self.cfg["url"].format(consulta=quote(CONSULTA % (lote, n))))
                for n in range(1, paginas + 1)], cursor

    def leer(self, fuente, d):
        try:
            datos = json.loads(d.texto(fuente.url))
        except json.JSONDecodeError as e:
            raise ErrorFormato("la consulta no devolvió JSON") from e
        errores = " ".join(str(e.get("message", "")) for e in datos.get("errors") or []
                           if isinstance(e, dict))
        if "greater than" in errores:
            return []  # página más allá del final del catálogo
        if errores or not isinstance(datos.get("data"), dict):
            raise ErrorFormato("respuesta inesperada de la consulta")
        items = ((datos["data"].get("products") or {}).get("items")) or []
        return [p for p in map(self._producto, items) if p]

    def _producto(self, it):
        if not isinstance(it, dict) or not it.get("url_key"):
            return None
        precios = ((it.get("price_range") or {}).get("minimum_price")) or {}
        precio = float(((precios.get("final_price") or {}).get("value")) or 0)
        antes = float(((precios.get("regular_price") or {}).get("value")) or 0)
        if precio <= 0:
            return None
        return Producto(
            tienda=self.clave,
            id=str(it.get("id")),
            nombre=limpiar(it.get("name")),
            url=self.cfg["base_producto"] + it["url_key"] + (it.get("url_suffix") or ""),
            precio=precio,
            precio_antes=antes if antes > precio else None,
            imagen=((it.get("small_image") or {}).get("url")),
            disponible=it.get("stock_status") != "OUT_OF_STOCK",
            vendedor=self.cfg.get("nombre"),
        )


_BLOQUE = re.compile(r'class="product-item-info[^"]*"\s+id="product-item-info_(\d+)"')
_ENLACE = re.compile(r'class="product-item-link"\s+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_IMAGEN = re.compile(r'<img class="product-image-photo"[^>]*\ssrc="([^"]+)"')
_PRECIO = re.compile(r'data-price-amount="([\d.]+)"\s+data-price-type="(finalPrice|oldPrice)"')
_VENDEDOR = re.compile(r'class="label-sold-by">\s*<span>[^<]*</span>\s*<span>([^<]+)</span>')


class MagentoListado(Tienda):

    def __init__(self, clave, cfg):
        super().__init__(clave, cfg)
        self._propios = {v.lower() for v in cfg.get("vendedores_propios", [])}

    def fuentes(self, cursor):
        url = self.cfg["url"]
        fijas = [Fuente(id=f"{self.clave}:{r}", url=url.format(ruta=r, pagina=1))
                 for r in self.cfg.get("fijas", [])]
        todas = [(cat, p) for cat, n in self.cfg.get("categorias", {}).items()
                 for p in range(1, int(n) + 1)]
        if not todas:
            return fijas, cursor
        por_revision = min(int(self.cfg.get("paginas_por_revision", 40)), len(todas))
        # "desfase" (0-1): dos tiendas con el mismo catálogo empiezan en puntos
        # distintos y entre ambas lo recorren en la mitad de tiempo.
        desfase = int(float(self.cfg.get("desfase", 0)) * len(todas))
        inicio = cursor % len(todas)
        desde = (inicio + desfase) % len(todas)
        tramo = (todas + todas)[desde:desde + por_revision]
        return fijas + [Fuente(id=f"{self.clave}:{cat}:p{p}", url=url.format(ruta=cat, pagina=p))
                        for cat, p in tramo], (inicio + por_revision) % len(todas)

    def leer(self, fuente, d):
        texto = d.texto(fuente.url)
        if "product-item-info" not in texto and "toolbar-number" not in texto:
            raise ErrorFormato("la página no tiene el listado esperado")
        marcas = list(_BLOQUE.finditer(texto))
        productos = []
        for i, m in enumerate(marcas):
            fin = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
            p = self._producto(m.group(1), texto[m.start():fin])
            if p:
                productos.append(p)
        return productos

    def _producto(self, id_, bloque):
        enlace = _ENLACE.search(bloque)
        precios = {tipo: float(v) for v, tipo in _PRECIO.findall(bloque)}
        precio = precios.get("finalPrice", 0)
        if not enlace or precio <= 0:
            return None
        antes = precios.get("oldPrice", 0)
        if re.fullmatch(r"9{3,}", f"{antes:.0f}"):
            antes = 0  # "precio habitual" de relleno (S/ 9 999): no es un precio real
        imagen = _IMAGEN.search(bloque)
        vendedor = _VENDEDOR.search(bloque)
        vendedor = limpiar(vendedor.group(1)) if vendedor else ""
        return Producto(
            tienda=self.clave,
            id=id_,
            nombre=limpiar(re.sub(r"<[^>]+>", "", enlace.group(2))),
            url=html.unescape(enlace.group(1)),
            precio=precio,
            precio_antes=antes if antes > precio else None,
            imagen=html.unescape(imagen.group(1)) if imagen else None,
            disponible=True,  # el listado solo muestra productos con stock
            vendedor=vendedor or None,
            marketplace=bool(vendedor) and vendedor.lower() not in self._propios,
        )
