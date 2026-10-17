"""Envío de alertas con la API oficial y gratuita de bots de Telegram."""

import html
import time
from datetime import datetime, timedelta, timezone

import httpx

from ofertas.analisis import Evaluacion
from ofertas.modelos import Producto

LIMA = timezone(timedelta(hours=-5))
LIMITE_CAPTION = 1024
LIMITE_MENSAJE = 4096


class ErrorTelegram(Exception):
    def __init__(self, mensaje, red=False):
        super().__init__(mensaje)
        self.red = red  # True si Telegram no respondió (no tiene sentido insistir)


def soles(x: float) -> str:
    return f"S/ {x:,.2f}"


def hora_lima(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), LIMA).strftime("%H:%M")


def texto_alerta(p: Producto, ev: Evaluacion, nombre_tienda: str) -> str:
    e = html.escape
    lineas = [f"<b>{ev.titulo}</b>", f"<b>{e(p.nombre[:180])}</b>", ""]
    if ev.referencia > p.precio:
        lineas.append(f"💰 Ahora: <b>{soles(p.precio)}</b>  (antes <s>{soles(ev.referencia)}</s>)")
        lineas.append(f"📉 <b>-{ev.ahorro_pct:.0%}</b> · ahorras {soles(ev.referencia - p.precio)}")
    else:
        lineas.append(f"💰 Ahora: <b>{soles(p.precio)}</b>")
    if p.precio_tarjeta:
        lineas.append(f"💳 Con tarjeta de la tienda: {soles(p.precio_tarjeta)}")
    tienda = f"🏬 {e(nombre_tienda)}"
    if p.vendedor:
        tienda += f" · vendido por {e(p.vendedor[:40])}"
    lineas.append(tienda)
    if p.stock is not None:
        lineas.append(f"📦 Stock: {p.stock} unidades" if p.stock <= 50 else "📦 Stock: disponible")
    elif p.disponible:
        lineas.append("📦 Stock: disponible (la tienda no informa unidades)")
    else:
        lineas.append("📦 Stock: la tienda no lo informa en el listado")
    lineas.append("")
    motivos = [f"• {e(m)}" for m in ev.motivos[:5]]
    pie = f"🕒 Detectado a las {hora_lima()} (hora de Lima)"
    # Si no cabe en el pie de foto, se quitan motivos (nunca se corta una etiqueta HTML).
    while motivos and len("\n".join(lineas + motivos + [pie])) > LIMITE_CAPTION:
        motivos.pop()
    return "\n".join(lineas + motivos + [pie])


def boton_comprar(url: str) -> dict:
    return {"inline_keyboard": [[{"text": "🛒 COMPRAR AHORA", "url": url}]]}


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.chat_id = str(chat_id)
        self._url = f"https://api.telegram.org/bot{token}/"
        self._cliente = httpx.Client(timeout=20)
        self._ultimo_envio = 0.0

    def cerrar(self):
        self._cliente.close()

    def llamar(self, metodo: str, **datos):
        for intento in (1, 2, 3):
            try:
                r = self._cliente.post(self._url + metodo, json=datos)
                cuerpo = r.json()
            except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
                if intento == 3:
                    raise ErrorTelegram(f"{metodo}: error de red ({e.__class__.__name__})",
                                        red=True) from e
                time.sleep(2)
                continue
            if cuerpo.get("ok"):
                return cuerpo.get("result")
            espera = (cuerpo.get("parameters") or {}).get("retry_after")
            if r.status_code == 429 and espera is not None and intento < 3:
                time.sleep(min(int(espera) + 1, 60))
                continue
            raise ErrorTelegram(f"{metodo}: {cuerpo.get('description', r.status_code)}")
        raise ErrorTelegram(f"{metodo}: sin respuesta", red=True)

    def _pausa(self):
        # Telegram admite ~1 mensaje por segundo en un mismo chat.
        espera = 1.1 - (time.monotonic() - self._ultimo_envio)
        if espera > 0:
            time.sleep(espera)
        self._ultimo_envio = time.monotonic()

    def enviar_alerta(self, p: Producto, ev: Evaluacion, nombre_tienda: str):
        texto = texto_alerta(p, ev, nombre_tienda)
        botones = boton_comprar(p.url)
        self._pausa()
        if p.imagen:
            try:
                return self.llamar("sendPhoto", chat_id=self.chat_id, photo=p.imagen,
                                   caption=texto, parse_mode="HTML", reply_markup=botones)
            except ErrorTelegram:
                pass  # p. ej. formato de imagen no aceptado: se envía como texto
        return self.enviar_texto(texto, botones=botones, vista_previa=p.imagen or p.url)

    def enviar_texto(self, texto: str, botones: dict | None = None, vista_previa: str | None = None):
        self._pausa()
        datos = {"chat_id": self.chat_id, "text": texto[:LIMITE_MENSAJE], "parse_mode": "HTML"}
        if botones:
            datos["reply_markup"] = botones
        if vista_previa:
            datos["link_preview_options"] = {"url": vista_previa, "prefer_large_media": True,
                                             "show_above_text": True}
        else:
            datos["link_preview_options"] = {"is_disabled": True}
        return self.llamar("sendMessage", **datos)

    def enviar_lista(self, encabezado: str, lineas: list[str]):
        """Envía una lista larga partida en mensajes de menos de 4096 caracteres."""
        bloque = encabezado
        for linea in lineas:
            if len(bloque) + len(linea) + 1 > LIMITE_MENSAJE - 50:
                self.enviar_texto(bloque)
                bloque = ""
            bloque += "\n" + linea
        if bloque.strip():
            self.enviar_texto(bloque)

    def actualizaciones(self, offset: int) -> list[dict]:
        return self.llamar("getUpdates", offset=offset, timeout=0,
                           allowed_updates=["message"]) or []
