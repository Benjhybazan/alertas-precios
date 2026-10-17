"""Lector para tiendas sobre la plataforma VTEX (catálogo en JSON, con el
stock que informa la tienda). La dirección de consulta y la profundidad vienen
de la configuración privada."""

import json

from ofertas.modelos import Fuente, Producto
from ofertas.tiendas.base import ErrorFormato, Tienda, limpiar

LOTE = 50  # máximo que permite la API por petición


class Vtex(Tienda):

    def _url(self, desde):
        return self.cfg["url"].format(dominio=self.cfg["dominio"], desde=desde,
                                      hasta=desde + LOTE - 1)

    def fuentes(self, cursor):
        fondo = int(self.cfg.get("profundidad", 1000))
        return [Fuente(id=f"{self.clave}:{a}", url=self._url(a)) for a in range(0, fondo, LOTE)], cursor

    def leer(self, fuente, d):
        try:
            datos = json.loads(d.texto(fuente.url))
        except json.JSONDecodeError as e:
            raise ErrorFormato("la API no devolvió JSON") from e
        if not isinstance(datos, list):
            raise ErrorFormato("respuesta inesperada de la API")
        return [x for p in datos if isinstance(p, dict) for x in self._productos(p)]

    def _productos(self, p):
        """Un Producto por variante (SKU) disponible, cada una con su historial:
        un mismo producto puede tener tallas o presentaciones de precios muy
        distintos."""
        if not p.get("link"):
            return []
        ofertas = []
        for item in p.get("items") or []:
            # De cada variante, el vendedor con el precio más bajo disponible.
            mejor = None
            for vend in item.get("sellers") or []:
                co = vend.get("commertialOffer") or {}
                precio = co.get("Price") or 0
                if precio > 0 and (co.get("AvailableQuantity") or 0) > 0 \
                        and (mejor is None or precio < mejor[1]["Price"]):
                    mejor = (vend, co)
            if mejor:
                ofertas.append((item, *mejor))
        nombre = limpiar(p.get("productName"))
        varios_precios = len({co["Price"] for _, _, co in ofertas}) > 1
        productos = []
        for item, vend, co in ofertas:
            imagenes = item.get("images") or []
            precio, lista = float(co["Price"]), float(co.get("ListPrice") or 0)
            productos.append(Producto(
                tienda=self.clave,
                id=f"{p.get('productId')}-{item.get('itemId')}",
                # Variantes al mismo precio (tallas) comparten nombre y se avisan
                # una sola vez; si los precios difieren, se indica cuál es.
                nombre=f"{nombre} ({limpiar(item.get('name'))})" if varios_precios else nombre,
                url=p["link"],
                precio=precio,
                precio_antes=lista if lista > precio else None,
                imagen=imagenes[0].get("imageUrl") if imagenes else None,
                stock=int(co.get("AvailableQuantity") or 0),
                disponible=True,
                vendedor=vend.get("sellerName"),
                marketplace=str(vend.get("sellerId")) != "1",
            ))
        return productos
