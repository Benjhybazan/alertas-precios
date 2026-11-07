"""Descarga de todas las tiendas en paralelo y unificación de sus productos."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ofertas.red import Descargador, ErrorDescarga
from ofertas.registro import log

# Códigos con los que una tienda pide que no se le insista.
CODIGOS_BLOQUEO = (403, 429)


def revisar_tienda(tienda, cursor, inicio, limite):
    """Lee las fuentes de una tienda empezando por la posición `inicio`.

    Si hay límite de tiempo (repositorio privado) y no alcanza, la siguiente
    ejecución continúa por las fuentes que quedaron sin leer. Si la tienda
    responde 403/429, todas sus conexiones paran (no se le insiste)."""
    fuentes, nuevo_cursor = tienda.fuentes(cursor)
    k = inicio % len(fuentes) if fuentes else 0
    cola, tomadas = iter(fuentes[k:] + fuentes[:k]), []
    candado, frenada = threading.Lock(), threading.Event()

    def siguiente():
        with candado:
            if frenada.is_set() or (limite and time.monotonic() > limite):
                return None
            f = next(cola, None)
            if f:
                tomadas.append(f)
            return f

    n = max(1, min(tienda.conexiones, len(fuentes)))
    with ThreadPoolExecutor(max_workers=n) as pool:
        partes = list(pool.map(lambda _: leer_fuentes(tienda, siguiente, limite, frenada),
                               range(n)))
    return {
        "lecturas": [x for lec, _ in partes for x in lec],
        "errores": [x for _, err in partes for x in err],
        "cursor": nuevo_cursor,
        "inicio": (k + len(tomadas)) % len(fuentes) if fuentes else 0,
        "sin_leer": len(fuentes) - len(tomadas),
    }


def leer_fuentes(tienda, siguiente, limite=None, frenada=None):
    """Una conexión: toma fuentes de la cola compartida hasta que se acaben."""
    lecturas, errores = [], []
    try:
        d = Descargador(usar_curl=tienda.usar_curl, limite=limite)
    except ErrorDescarga as e:
        return [], [str(e)]
    try:
        while (f := siguiente()) is not None:
            try:
                lecturas.append((f, tienda.leer(f, d)))
            except ErrorDescarga as e:
                errores.append(f"{f.id}: {e}")
                if e.codigo in CODIGOS_BLOQUEO:
                    if frenada is not None:
                        frenada.set()  # la tienda nos frena: ninguna conexión insiste
                    break
            except Exception as e:  # cambio en la web: no debe tumbar la ejecución
                errores.append(f"{f.id}: formato inesperado ({e.__class__.__name__}: {e})")
    finally:
        d.cerrar()
    return lecturas, errores


def descargar(tiendas, est, limite):
    """Descarga todas las tiendas en paralelo (cada una a su propio ritmo)."""
    resultados = {}
    with ThreadPoolExecutor(max_workers=max(1, len(tiendas))) as pool:
        trabajos = {t.clave: pool.submit(revisar_tienda, t, est["cursores"].get(t.clave, 0),
                                         est["inicios"].get(t.clave, 0), limite)
                    for t in tiendas}
        for clave, tr in trabajos.items():
            try:
                resultados[clave] = tr.result()
            except Exception as e:
                resultados[clave] = {
                    "lecturas": [], "errores": [f"error interno: {e.__class__.__name__}: {e}"],
                    "cursor": est["cursores"].get(clave, 0), "inicio": est["inicios"].get(clave, 0),
                    "sin_leer": 0}
    return resultados


def url_valida(url) -> bool:
    return isinstance(url, str) and url.startswith(("https://", "http://"))


def unificar(resultados, est, nombres, ahora):
    """Un producto puede salir en varias fuentes: se cuenta una sola vez.

    linea_base[clave] es True si TODAS sus fuentes se leen por primera vez.
    Los productos sin enlace válido se descartan (no se podrían comprar)."""
    productos, linea_base = {}, {}
    for clave, r in resultados.items():
        est["cursores"][clave], est["inicios"][clave] = r["cursor"], r["inicio"]
        total = sum(len(ps) for _, ps in r["lecturas"])
        log(f"{nombres[clave]}: {len(r['lecturas'])} consultas, {total} productos, "
            f"{len(r['errores'])} errores"
            + (f", {r['sin_leer']} quedan para la próxima revisión" if r["sin_leer"] else ""),
            detalle=True)
        for err in r["errores"][:5]:
            log(f"   ! {err}", detalle=True)
        for fuente, ps in r["lecturas"]:
            nueva = fuente.id not in est["fuentes"]
            for p in ps:
                if not url_valida(p.url):
                    continue
                p.origen = p.origen or fuente.id
                if p.clave not in productos:
                    productos[p.clave] = p
                    linea_base[p.clave] = nueva
                else:
                    linea_base[p.clave] = linea_base[p.clave] and nueva
            est["fuentes"][fuente.id] = int(ahora)
    return productos, linea_base
