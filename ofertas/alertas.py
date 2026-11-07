"""Elección y envío de alertas: qué productos avisar, cuáles con foto, cuándo
sin sonido, y qué queda pendiente si algo falla."""

import html
import re
import time
from datetime import datetime

import config
from ofertas import analisis
from ofertas.registro import log
from ofertas.telegram import LIMA, ErrorTelegram, partir_lista, soles, texto_alerta
from ofertas.tiendas.base import slug

# Las mejores alertas se envían siempre, aunque se haya acabado el tiempo.
ALERTAS_GARANTIZADAS = 3
# Sin precio máximo, un producto vigilado avisa desde este descuento.
DESCUENTO_VIGILADO = 0.40
RECIENTES_SEGUNDOS = 86400
MAX_RECIENTES = 150
MAX_AVISADOS_POR_VIGILADO = 200


def sin_html(texto):
    """Versión para consola de un mensaje con formato HTML de Telegram."""
    return html.unescape(re.sub(r"<[^>]+>", "", texto))


def firma(p):
    """Mismo artículo al mismo precio, aunque lo publiquen varias tiendas."""
    return f"{slug(p.nombre)[:60]}|{round(p.precio)}"


def palabras(texto: str) -> list[str]:
    return [w for w in slug(texto).split("-") if w]


def coincide(nombre_slug: str, buscadas: list[str]) -> bool:
    """Todas las palabras buscadas aparecen en el nombre (ya pasado por slug)."""
    return bool(buscadas) and all(w in nombre_slug for w in buscadas)


def descuento(p) -> float:
    return 1 - p.precio / p.precio_antes if p.precio_antes and p.precio_antes > p.precio else 0.0


# ---------------------------------------------------------------------------
# Elección
# ---------------------------------------------------------------------------

def elegir_alertas(productos, linea_base, est, ahora, marcar_pendientes, evaluaciones=None):
    """Evalúa cada producto, registra su historial y devuelve las alertas a enviar.
    En `evaluaciones` deja las ofertas válidas (las usan /top y /buscar)."""
    candidatas = []
    for clave, p in productos.items():
        est_p = est["productos"].get(clave)
        ev = analisis.evaluar(p, (est_p or {}).get("h"), ahora)
        if ev is not None and evaluaciones is not None:
            evaluaciones[clave] = ev
        if ev and analisis.es_novedad(p, ev, est_p, linea_base[clave]):
            candidatas.append((p, ev))
        elif est_p and not ev:
            est_p.pop("pend", None)  # ya no es oferta: no queda nada pendiente
        analisis.registrar(est["productos"].setdefault(clave, {}), p, ahora)

    alertas, firmas = [], set()
    for p, ev in sorted(candidatas, key=lambda x: (-x[1].nivel, -x[1].ahorro_pct)):
        est_p = est["productos"][p.clave]
        f = firma(p)
        previo = est["avisos_recientes"].get(f)
        otra_tienda = bool(previo) and previo[1] != p.clave
        if f in firmas or (otra_tienda and not est_p.get("pend")):
            # Mismo artículo ya avisado en esta ejecución o desde otra tienda.
            est_p["a"] = {"t": int(ahora), "p": p.precio, "n": ev.nivel}
            continue
        firmas.add(f)
        if marcar_pendientes:
            # Queda pendiente hasta confirmar el envío: si algo falla a mitad
            # de camino, se reintenta en la siguiente ejecución.
            est_p["pend"] = 1
        alertas.append((p, ev))
    return alertas


def alertas_vigiladas(productos, evaluaciones, est, ya_incluidas):
    """Productos que coinciden con /vigilar: precio máximo pedido o, si no se dio,
    un descuento de al menos DESCUENTO_VIGILADO. Se avisa una vez por producto
    (otra vez solo si baja un 5 % más)."""
    vigilados = list(est.get("vigilar") or [])
    if not vigilados:
        return []
    candidatos = [(clave, p, slug(p.nombre)) for clave, p in productos.items()
                  if p.disponible is not False and p.precio > 0]
    alertas = []
    for v in vigilados:
        buscadas = palabras(v["q"])
        for clave, p, nombre_slug in candidatos:
            if clave in ya_incluidas or not coincide(nombre_slug, buscadas):
                continue
            pct = descuento(p)
            if v.get("max") is not None:
                if p.precio > v["max"]:
                    continue
            elif pct < DESCUENTO_VIGILADO:
                continue
            previo = (v.get("avisados") or {}).get(clave)
            if previo and p.precio > previo * (1 - config.BAJADA_PARA_REAVISAR):
                continue
            base = evaluaciones.get(clave)
            ev = analisis.Evaluacion(
                nivel=base.nivel if base else analisis.GRANDE, descuento=pct,
                caida=base.caida if base else 0.0, habitual=base.habitual if base else None,
                referencia=max(p.precio_antes or 0, p.precio), motivos=base.motivos if base else [],
                minimo=base.minimo if base else None, veredicto=base.veredicto if base else "",
                vigilado=v["q"])
            alertas.append((p, ev))
            ya_incluidas.add(clave)
    return alertas


# ---------------------------------------------------------------------------
# Prioridad y sonido
# ---------------------------------------------------------------------------

_RE_INTERES = {}


def _patron_interes(campo):
    """Regex de intereses compilada una vez; si es inválida se ignora (con aviso)."""
    if campo not in _RE_INTERES:
        patron = config.INTERESES.get(campo)
        try:
            _RE_INTERES[campo] = re.compile(patron, re.I) if patron else None
        except re.error:
            log(f"Intereses: la expresión de '{campo}' no es válida; se ignora.")
            _RE_INTERES[campo] = None
    return _RE_INTERES[campo]


def es_de_interes(p):
    """¿Coincide con tus intereses (por nombre o por la categoría donde apareció)?"""
    if not config.INTERESES:
        return True
    for campo, texto in (("nombre", p.nombre), ("fuente", p.origen)):
        patron = _patron_interes(campo)
        if patron and patron.search(texto or ""):
            return True
    return False


def es_prioritaria(p, ev):
    """Llega sola, con foto: tus intereses, lo que vigilas, descuentos enormes o
    posibles errores."""
    return (bool(ev.vigilado) or es_de_interes(p) or ev.ahorro_pct >= config.DESCUENTO_PRIORITARIO
            or ev.nivel == analisis.ERROR)


def en_descanso(ahora):
    """¿Estamos en el horario de descanso (hora de Lima)?"""
    try:
        inicio, fin = [int(h) * 60 + int(m) for h, m in
                       (x.split(":") for x in config.DESCANSO.split("-"))]
    except ValueError:
        return False
    local = datetime.fromtimestamp(ahora, LIMA)
    minuto = local.hour * 60 + local.minute
    return inicio <= minuto or minuto < fin if inicio > fin else inicio <= minuto < fin


def silenciado(est, ahora):
    """¿El usuario pidió /silencio y aún no termina?"""
    return est.get("silencio_hasta", 0) > ahora


# ---------------------------------------------------------------------------
# Envío
# ---------------------------------------------------------------------------

def marcar_avisado(est, p, ev, ahora):
    est_p = est["productos"].setdefault(p.clave, {})
    est_p["a"] = {"t": int(ahora), "p": p.precio, "n": ev.nivel}
    est_p.pop("pend", None)
    est["avisos_recientes"][firma(p)] = [int(ahora), p.clave]
    if ev.vigilado:
        # Copia: los comandos pueden cambiar la lista desde otro hilo.
        for v in list(est.get("vigilar") or []):
            if v["q"] == ev.vigilado:
                avisados = v.setdefault("avisados", {})
                avisados[p.clave] = p.precio
                if len(avisados) > MAX_AVISADOS_POR_VIGILADO:
                    del avisados[next(iter(avisados))]
    # Para /hoy: las alertas de las últimas 24 h.
    recientes = list(est.get("recientes") or [])
    recientes.append([int(ahora), p.nombre[:80], p.precio, round(ev.ahorro_pct, 3), p.url, p.tienda])
    est["recientes"] = [r for r in recientes if ahora - r[0] < RECIENTES_SEGUNDOS][-MAX_RECIENTES:]


def enviar_alertas(tg, alertas, nombres, est, ahora, limite):
    """Tus intereses, los descuentos enormes y los posibles errores van con foto y
    botón; el resto, juntos en un mensaje-lista. Las que no se logren enviar por
    un fallo pasajero quedan pendientes ("pend") para la siguiente vez; las que
    Telegram rechaza siempre se descartan (si no, se reintentarían para siempre)."""
    prioritarias = [a for a in alertas if es_prioritaria(*a)]
    otras = [a for a in alertas if not es_prioritaria(*a)]
    individuales = prioritarias[:config.MAX_ALERTAS_POR_EJECUCION]
    resto = sorted(prioritarias[config.MAX_ALERTAS_POR_EJECUCION:] + otras,
                   key=lambda a: -a[1].ahorro_pct)
    enviadas = 0
    for p, ev in individuales:
        if tg is None:
            print("\n" + sin_html(texto_alerta(p, ev, nombres[p.tienda])))
            print(f"🛒 {p.url}\n" + "-" * 60)
            enviadas += 1
            continue
        if enviadas >= ALERTAS_GARANTIZADAS and time.monotonic() > limite:
            log("Sin tiempo para más alertas: quedan pendientes para la próxima revisión.")
            return enviadas
        try:
            # De noche suenan solo los posibles errores de precio de productos que
            # no son baratos; con /silencio no suena nada.
            alarma = ev.nivel == analisis.ERROR and ev.referencia >= config.MIN_PRECIO_REFERENCIA
            silencioso = silenciado(est, ahora) or (en_descanso(ahora) and not alarma)
            tg.enviar_alerta(p, ev, nombres[p.tienda], silencioso=silencioso)
            marcar_avisado(est, p, ev, ahora)
            enviadas += 1
        except ErrorTelegram as e:
            log(f"No se pudo enviar la alerta de {p.clave}: {e}", detalle=True)
            if e.red:
                return enviadas  # Telegram no responde: no insistir con el resto
            marcar_avisado(est, p, ev, ahora)  # Telegram la rechaza: no reintentar
    if not resto:
        return enviadas
    lineas = [f"<b>-{ev.ahorro_pct:.0%}</b> {soles(p.precio)} · "
              f"<a href=\"{html.escape(p.url)}\">{html.escape(p.nombre[:60])}</a> "
              f"<i>{html.escape(nombres[p.tienda])}</i>"
              for p, ev in resto]
    encabezado = f"📋 <b>Otras ofertas ({len(resto)})</b>"
    if tg is None:
        print(sin_html(encabezado + "\n" + "\n".join(lineas)))
        return enviadas + len(resto)
    # Cada parte de la lista se da por avisada al enviarse: si una falla, solo
    # sus productos (y los siguientes) quedan pendientes, sin repetir los demás.
    hechas = desde = 0
    for texto, cantidad in partir_lista(encabezado, lineas):
        if time.monotonic() > limite:
            break
        parte, desde = resto[desde:desde + cantidad], desde + cantidad
        try:
            tg.enviar_texto(texto, silencioso=True)  # la lista nunca suena
            hechas += cantidad
        except ErrorTelegram as e:
            log(f"No se pudo enviar el resumen: {e}")
            if e.red:
                break  # pasajero: esta parte y las siguientes quedan pendientes
            # Rechazada por Telegram: se descarta para no bloquear las siguientes.
        for p, ev in parte:
            marcar_avisado(est, p, ev, ahora)
    return enviadas + hechas
