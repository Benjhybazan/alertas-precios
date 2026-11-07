"""Envío de alertas con la API oficial y gratuita de bots de Telegram."""

import html
import time
from datetime import datetime, timedelta, timezone

import httpx

import config
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
    """Mensaje corto: lo importante en la 1.ª línea (%, precio, tienda)."""
    e = html.escape
    emoji = "👁" if ev.vigilado else ev.titulo.split()[0]
    etiqueta = " ¿ERROR?" if emoji == "🚨" else ""
    pct = f"-{ev.ahorro_pct:.0%} · " if ev.referencia > p.precio else ""
    lineas = [f"{emoji}<b>{etiqueta} {pct}{soles(p.precio)}</b> · {e(nombre_tienda)}",
              e(p.nombre[:90])]
    if ev.referencia > p.precio:
        lineas.append(f"<s>{soles(ev.referencia)}</s> → ahorras <b>{soles(ev.referencia - p.precio)}</b>")
    if ev.veredicto:
        lineas.append(e(ev.veredicto))
    extras = ([f"Vigilando: {ev.vigilado}"] if ev.vigilado else []) + list(ev.motivos)
    if p.precio_tarjeta:
        extras.append(f"💳 {soles(p.precio_tarjeta)} con tarjeta")
    if p.stock is not None and p.stock > config.POCAS_UNIDADES and p.stock <= 50:
        extras.append(f"📦 {p.stock} unid.")
    if extras:
        lineas.append(" · ".join(e(x) for x in extras[:4]))
    texto = "\n".join(lineas)
    return texto[:LIMITE_CAPTION]


def partir_lista(encabezado: str, lineas: list[str]) -> list[tuple[str, int]]:
    """Parte una lista en mensajes que caben en Telegram: [(texto, n.º de líneas)]."""
    partes, bloque, cantidad = [], encabezado, 0
    for linea in lineas:
        linea = linea[:LIMITE_MENSAJE - 100]
        if len(bloque) + len(linea) + 1 > LIMITE_MENSAJE - 50:
            if bloque.strip():
                partes.append((bloque.strip("\n"), cantidad))
            bloque, cantidad = "", 0
        bloque += "\n" + linea
        cantidad += 1
    if bloque.strip():
        partes.append((bloque.strip("\n"), cantidad))
    return partes


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

    def llamar(self, metodo: str, _espera: float = 0, **datos):
        """_espera: segundos extra que Telegram puede tardar (long polling).

        ErrorTelegram.red=True: fallo pasajero (sin conexión, saturación de
        Telegram); False: Telegram rechazó la petición (reintentar no sirve)."""
        for intento in (1, 2, 3):
            try:
                r = self._cliente.post(self._url + metodo, json=datos,
                                       timeout=20 + _espera)
                cuerpo = r.json()
                if not isinstance(cuerpo, dict):
                    raise ValueError("respuesta sin formato")
            except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
                if intento == 3:
                    raise ErrorTelegram(f"{metodo}: error de red ({e.__class__.__name__})",
                                        red=True) from e
                time.sleep(2)
                continue
            if cuerpo.get("ok"):
                return cuerpo.get("result")
            pasajero = r.status_code == 429 or r.status_code >= 500
            if pasajero and intento < 3:
                espera = (cuerpo.get("parameters") or {}).get("retry_after")
                time.sleep(min(int(espera or 1) + 1, 60))
                continue
            raise ErrorTelegram(f"{metodo}: {cuerpo.get('description', r.status_code)}",
                                red=pasajero)
        raise ErrorTelegram(f"{metodo}: sin respuesta", red=True)

    def _pausa(self):
        # Telegram admite ~1 mensaje por segundo en un mismo chat.
        espera = 1.1 - (time.monotonic() - self._ultimo_envio)
        if espera > 0:
            time.sleep(espera)
        self._ultimo_envio = time.monotonic()

    def enviar_alerta(self, p: Producto, ev: Evaluacion, nombre_tienda: str,
                      silencioso: bool = False):
        """Con foto y botón; si Telegram rechaza la foto, como texto con vista
        previa; si rechaza el botón (enlace raro), con el enlace en el texto."""
        texto = texto_alerta(p, ev, nombre_tienda)
        botones = boton_comprar(p.url)
        self._pausa()
        if p.imagen:
            try:
                return self.llamar("sendPhoto", chat_id=self.chat_id, photo=p.imagen,
                                   caption=texto, parse_mode="HTML", reply_markup=botones,
                                   disable_notification=silencioso)
            except ErrorTelegram as e:
                if e.red:
                    raise  # sin conexión: los demás intentos también fallarían
        try:
            return self.enviar_texto(texto, botones=botones, vista_previa=p.imagen or p.url,
                                     silencioso=silencioso)
        except ErrorTelegram as e:
            if e.red:
                raise
        enlace = f'\n<a href="{html.escape(p.url)}">🛒 Comprar</a>'
        return self.enviar_texto(texto[:LIMITE_MENSAJE - len(enlace)] + enlace,
                                 silencioso=silencioso)

    def enviar_texto(self, texto: str, botones: dict | None = None, vista_previa: str | None = None,
                     silencioso: bool = False):
        """silencioso=True: llega al chat sin sonido ni vibración."""
        self._pausa()
        datos = {"chat_id": self.chat_id, "text": texto[:LIMITE_MENSAJE], "parse_mode": "HTML",
                 "disable_notification": silencioso}
        if botones:
            datos["reply_markup"] = botones
        if vista_previa:
            datos["link_preview_options"] = {"url": vista_previa, "prefer_large_media": True,
                                             "show_above_text": True}
        else:
            datos["link_preview_options"] = {"is_disabled": True}
        return self.llamar("sendMessage", **datos)

    def enviar_lista(self, encabezado: str, lineas: list[str], silencioso: bool = False):
        """Envía una lista larga partida en mensajes de menos de 4096 caracteres."""
        for texto, _ in partir_lista(encabezado, lineas):
            self.enviar_texto(texto, silencioso=silencioso)

    def actualizaciones(self, offset: int, espera: int = 0) -> list[dict]:
        """espera > 0: Telegram retiene la respuesta hasta que llegue un mensaje
        (o pasen esos segundos), así los comandos se responden al instante."""
        return self.llamar("getUpdates", _espera=espera, offset=offset, timeout=espera,
                           allowed_updates=["message", "callback_query"]) or []
