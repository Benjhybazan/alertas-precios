"""Comandos de Telegram: el bot escucha mientras dura cada revisión en la nube
(con "long polling"), así que responde en segundos y no cada pocos minutos.

Solo atiende al chat del dueño; los demás mensajes se ignoran."""

import html
import math
import re
import threading
import time
from datetime import datetime

import config
from ofertas.alertas import DESCUENTO_VIGILADO, coincide, descuento, palabras
from ofertas.registro import log
from ofertas.telegram import LIMA, ErrorTelegram, soles
from ofertas.tiendas.base import slug

# Lista que aparece al escribir "/" en Telegram (se registra una vez por versión).
VERSION_MENU = 1
MENU = [
    ("menu", "Botones de acceso rápido"),
    ("top", "Lo más rebajado ahora mismo"),
    ("buscar", "Buscar un producto: /buscar iphone"),
    ("hoy", "Alertas enviadas en las últimas 24 h"),
    ("vigilar", "Avisarme de un producto: /vigilar airpods hasta 300"),
    ("vigilados", "Ver o quitar lo que vigilo"),
    ("silencio", "Alertas sin sonido: /silencio 2 (horas)"),
    ("sonido", "Volver a activar el sonido"),
    ("estado", "Ver si el bot y las tiendas funcionan"),
    ("ayuda", "Qué puedo hacer"),
]
AYUDA = (
    "🤖 <b>Comandos</b>\n"
    "/top — lo más rebajado ahora mismo\n"
    "/buscar <i>texto</i> — busca entre las ofertas revisadas (o escribe el texto sin /)\n"
    "/hoy — alertas de las últimas 24 h\n"
    "/vigilar <i>texto</i> [precio máx.] — te aviso cuando aparezca\n"
    "   ej.: <code>/vigilar airpods hasta 300</code> · <code>/vigilar zapatillas nike</code>\n"
    "/vigilados — ver o quitar lo que vigilo\n"
    "/silencio [horas] — alertas sin sonido (2 h por defecto) · /sonido\n"
    "/estado — tiendas y última revisión\n"
    "/menu — botones de acceso rápido"
)
BOTONES_MENU = {"inline_keyboard": [
    [{"text": "🔥 Top ahora", "callback_data": "top"}, {"text": "📅 Hoy", "callback_data": "hoy"}],
    [{"text": "👁 Vigilados", "callback_data": "vigilados"},
     {"text": "📊 Estado", "callback_data": "estado"}],
    [{"text": "🔕 Silencio 2 h", "callback_data": "silencio 2"},
     {"text": "❓ Ayuda", "callback_data": "ayuda"}],
]}

MAX_VIGILADOS = 20
MAX_RESULTADOS = 10
ESPERA_REVISION = 90  # segundos máximos esperando la revisión en curso
PAUSA_TRAS_FALLO = 5  # segundos antes de volver a escuchar si Telegram no responde
# Texto suelto que se entiende como comando aunque no empiece por "/".
PALABRAS_COMANDO = ("top", "hoy", "vigilados", "estado", "ayuda", "silencio", "sonido", "olvidar")


def linea(p, pct, nombre_tienda, marca=""):
    return (f"{marca}<b>-{pct:.0%}</b> {soles(p.precio)} · "
            f"<a href=\"{html.escape(p.url)}\">{html.escape(p.nombre[:60])}</a> "
            f"<i>{html.escape(nombre_tienda)}</i>")


def leer_vigilar(texto: str):
    """'airpods pro hasta 300' / 'airpods pro S/300' -> ('airpods pro', 300.0).
    Un número suelto no es precio (iphone 15, rtx 4090): sin precio, None."""
    m = re.search(r"\s+(?:hasta|max(?:imo)?|m[aá]x\.?|<=?|s/\.?)\s*(?:s/\.?\s*)?([\d.,]+)\s*$",
                  " " + texto.strip(), re.I)
    maximo = None
    if m:
        try:
            maximo = float(m.group(1).replace(",", ""))
            texto = (" " + texto.strip())[:m.start()]
        except ValueError:
            pass
    return texto.strip()[:60], maximo


class Escucha(threading.Thread):
    """Atiende comandos en segundo plano mientras la ejecución revisa tiendas."""

    def __init__(self, tg, est, nombres, texto_estado):
        super().__init__(daemon=True)
        self.tg, self.est, self.nombres = tg, est, nombres
        self.texto_estado = texto_estado
        self.fin = None                   # hora (monotonic) a la que dejar de escuchar
        self._parar = threading.Event()
        self._revision = threading.Event()
        self.productos, self.evaluaciones = {}, {}
        self._indice = None               # [(producto, nombre en slug)] para /buscar
        self.error = None

    # --- control desde la ejecución principal ---

    def publicar_revision(self, productos, evaluaciones):
        """Productos de la revisión en curso: con ellos responden /top y /buscar."""
        self.productos, self.evaluaciones = productos, evaluaciones
        self._indice = None
        self._revision.set()

    def terminar(self, hasta=None):
        """Escucha hasta `hasta` (monotonic) y termina; sin `hasta`, de inmediato."""
        self.fin = hasta if hasta is not None else time.monotonic()
        if self.fin <= time.monotonic():
            self._parar.set()
        self._revision.set()  # nadie debe quedarse esperando
        self.join()

    # --- bucle ---

    def run(self):
        """Escucha hasta `fin`. Un fallo de red no termina la escucha (se reintenta
        tras una pausa) y un comando que falla no afecta a los siguientes."""
        self._registrar_menu()
        while True:
            restante = (self.fin - time.monotonic()) if self.fin is not None else 20
            ultima = restante <= 0 or self._parar.is_set()
            espera = 0 if ultima else int(max(1, min(20, restante)))
            try:
                actualizaciones = self.tg.actualizaciones(self.est.get("telegram_offset", 0), espera)
            except ErrorTelegram as e:
                self.error = e
                log(f"Telegram: {e}")
                if ultima or self._parar.wait(PAUSA_TRAS_FALLO):
                    return
                continue
            nuevas = 0
            for upd in actualizaciones:
                if not isinstance(upd, dict) or not isinstance(upd.get("update_id"), int):
                    continue
                if upd["update_id"] < self.est.get("telegram_offset", 0):
                    continue
                self.est["telegram_offset"] = upd["update_id"] + 1
                nuevas += 1
                try:
                    self.atender(upd)
                except ErrorTelegram as e:
                    log(f"Telegram: {e}")
                except Exception as e:  # un comando raro no debe apagar la escucha
                    log(f"Comando no atendido ({e.__class__.__name__})")
            if ultima:
                return
            if not nuevas:
                self._parar.wait(1)  # por si Telegram responde sin esperar

    def _registrar_menu(self):
        if self.est.get("menu_comandos") == VERSION_MENU:
            return
        try:
            self.tg.llamar("setMyCommands",
                           commands=[{"command": c, "description": d} for c, d in MENU])
            self.est["menu_comandos"] = VERSION_MENU
        except ErrorTelegram:
            pass  # se reintenta en la siguiente ejecución

    # --- comandos ---

    def atender(self, upd):
        cq = upd.get("callback_query")
        if cq:
            chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
            if str(chat) != self.tg.chat_id:
                return
            try:
                self.tg.llamar("answerCallbackQuery", callback_query_id=cq["id"])
            except ErrorTelegram:
                pass
            texto = cq.get("data") or ""
        else:
            msg = upd.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != self.tg.chat_id:
                return
            texto = (msg.get("text") or "").strip()
        if not texto:
            return
        if texto.startswith("/"):
            comando, _, resto = texto[1:].partition(" ")
            comando = comando.split("@")[0].lower()
        else:
            comando, resto = texto.split(" ", 1)[0].lower(), ""
            if comando not in PALABRAS_COMANDO:
                comando, resto = "buscar", texto  # texto suelto = búsqueda
            else:
                resto = texto.partition(" ")[2]
        resto = resto.strip()
        acciones = {
            "start": self.menu, "menu": self.menu, "ayuda": self.ayuda, "help": self.ayuda,
            "estado": self.estado, "status": self.estado, "top": self.top,
            "buscar": self.buscar, "hoy": self.hoy, "vigilar": self.vigilar,
            "vigilados": self.vigilados, "olvidar": self.olvidar,
            "silencio": self.silencio, "sonido": self.sonido,
        }
        accion = acciones.get(comando)
        if accion:
            accion(resto)
        else:
            self.tg.enviar_texto("No conozco ese comando. Escribe /ayuda para ver la lista.")

    def menu(self, _=""):
        self.tg.enviar_texto("🤖 <b>Bot de ofertas</b> — ¿qué quieres ver?\n"
                             "También puedes escribir un producto (ej.: <i>televisor 55</i>) "
                             "y lo busco.", botones=BOTONES_MENU)

    def ayuda(self, _=""):
        self.tg.enviar_texto(AYUDA)

    def estado(self, _=""):
        self.tg.enviar_texto(self.texto_estado(self.est, self.nombres, time.time()))

    def _esperar_revision(self):
        if not self._revision.is_set():
            self.tg.enviar_texto("⏳ Revisando las tiendas, te respondo en unos segundos…")
            self._revision.wait(ESPERA_REVISION)
        return self.productos

    def top(self, _=""):
        productos = self._esperar_revision()
        if not productos:
            self.tg.enviar_texto("Todavía no tengo productos de esta revisión. Prueba en un minuto.")
            return
        def pct(p):
            return max(descuento(p), self._ahorro(p))

        def fila(p):
            return linea(p, pct(p), self.nombres.get(p.tienda, p.tienda),
                         "✅ " if p.clave in self.evaluaciones else "")

        # Los baratísimos (S/ 1 con 95 %) van aparte para no tapar las grandes ofertas.
        con_rebaja = [p for p in productos.values() if pct(p) > 0]
        grandes = sorted((p for p in con_rebaja
                          if max(p.precio_antes or 0, p.precio) >= config.MIN_PRECIO_REFERENCIA),
                         key=lambda p: -pct(p))[:15]
        baratos = sorted((p for p in con_rebaja if p not in grandes
                          and max(p.precio_antes or 0, p.precio) < config.MIN_PRECIO_REFERENCIA
                          and pct(p) >= config.DESCUENTO_SIN_MINIMO),
                         key=lambda p: -pct(p))[:5]
        lineas = [fila(p) for p in grandes]
        if baratos:
            lineas += ["", "💸 <b>Baratísimos (≥90 %)</b>"] + [fila(p) for p in baratos]
        self.tg.enviar_lista(
            f"🔥 <b>Lo más rebajado ahora</b> ({len(productos):,} productos revisados)\n"
            "✅ = el bot confirma que es una oferta real", lineas)

    def _buscables(self):
        """Nombres de la revisión pasados por slug una sola vez (búsquedas rápidas)."""
        if self._indice is None:
            self._indice = [(p, slug(p.nombre)) for p in self.productos.values()]
        return self._indice

    def _ahorro(self, p):
        ev = self.evaluaciones.get(p.clave)
        return ev.ahorro_pct if ev else 0.0

    def buscar(self, texto):
        buscadas = palabras(texto)
        if not buscadas:
            self.tg.enviar_texto("Escribe qué buscar, por ejemplo: <code>/buscar airpods</code>")
            return
        productos = self._esperar_revision()
        hallados = sorted((p for p, nombre in self._buscables() if coincide(nombre, buscadas)),
                          key=lambda p: -max(descuento(p), self._ahorro(p)))
        consulta = html.escape(texto[:60])
        if not hallados:
            self.tg.enviar_texto(
                f"🔎 No encontré «{consulta}» entre las {len(productos):,} ofertas revisadas ahora.\n"
                f"Usa <code>/vigilar {consulta}</code> y te aviso cuando aparezca.")
            return
        lineas = [linea(p, max(descuento(p), self._ahorro(p)), self.nombres.get(p.tienda, p.tienda),
                        "✅ " if p.clave in self.evaluaciones else "")
                  for p in hallados[:MAX_RESULTADOS]]
        self.tg.enviar_texto(
            f"🔎 <b>{consulta}</b>: {len(hallados)} resultado(s), mayor descuento primero\n"
            + "\n".join(lineas),
            botones={"inline_keyboard": [[{"text": f"👁 Vigilar «{texto[:30]}»",
                                           "callback_data": ("vigilar " + texto).encode()[:64]
                                           .decode(errors="ignore")}]]})

    def hoy(self, _=""):
        ahora = time.time()
        recientes = [r for r in self.est.get("recientes", []) if ahora - r[0] < 86400]
        if not recientes:
            self.tg.enviar_texto("📅 Sin alertas en las últimas 24 h.")
            return
        lineas = []
        for t, nombre, precio, pct, url, tienda in sorted(recientes, key=lambda r: -r[3])[:40]:
            hora = datetime.fromtimestamp(t, LIMA).strftime("%H:%M")
            lineas.append(f"<b>-{pct:.0%}</b> {soles(precio)} · <a href=\"{html.escape(url)}\">"
                          f"{html.escape(nombre[:55])}</a> <i>{html.escape(self.nombres.get(tienda, tienda))} "
                          f"{hora}</i>")
        self.tg.enviar_lista(f"📅 <b>Alertas de las últimas 24 h ({len(recientes)})</b>", lineas)

    def vigilar(self, texto):
        consulta, maximo = leer_vigilar(texto)
        if not palabras(consulta):
            self.tg.enviar_texto("Dime qué vigilar, por ejemplo:\n"
                                 "<code>/vigilar airpods hasta 300</code> (precio máximo S/ 300)\n"
                                 "<code>/vigilar zapatillas nike</code> (desde "
                                 f"{DESCUENTO_VIGILADO:.0%} de descuento)")
            return
        lista = self.est.setdefault("vigilar", [])
        repetido = next((v for v in lista if palabras(v["q"]) == palabras(consulta)), None)
        if repetido:  # misma búsqueda (o comando repetido): se actualiza, no se duplica
            repetido["max"] = maximo
            self.tg.enviar_texto(f"👁 Ya vigilaba «{html.escape(repetido['q'])}»: lo actualicé.")
            return
        if len(lista) >= MAX_VIGILADOS:
            self.tg.enviar_texto(f"Ya vigilo {MAX_VIGILADOS} productos. Quita alguno con /vigilados.")
            return
        nuevo_id = max((v["id"] for v in lista), default=0) + 1
        lista.append({"id": nuevo_id, "q": consulta, "max": maximo, "avisados": {}})
        condicion = (f"a {soles(maximo)} o menos" if maximo
                     else f"con {DESCUENTO_VIGILADO:.0%} de descuento o más")
        self.tg.enviar_texto(f"👁 Vigilando «{html.escape(consulta)}»: te aviso cuando aparezca "
                             f"{condicion} (revisión cada ~5 min).")

    def vigilados(self, _=""):
        lista = self.est.get("vigilar") or []
        if not lista:
            self.tg.enviar_texto("No vigilo nada todavía. Ejemplo: <code>/vigilar airpods hasta 300</code>")
            return
        lineas = [f"{v['id']}. {html.escape(v['q'])}"
                  + (f" ≤ {soles(v['max'])}" if v.get("max") else f" (≥{DESCUENTO_VIGILADO:.0%})")
                  for v in lista]
        botones = [[{"text": f"❌ Quitar {v['id']}. {v['q'][:25]}", "callback_data": f"olvidar {v['id']}"}]
                   for v in lista]
        self.tg.enviar_texto("👁 <b>Vigilando</b>\n" + "\n".join(lineas),
                             botones={"inline_keyboard": botones})

    def olvidar(self, texto):
        lista = self.est.get("vigilar") or []
        try:
            numero = int(texto.split()[0])
        except (ValueError, IndexError):
            self.tg.enviar_texto("Indica el número, por ejemplo <code>/olvidar 1</code> (míralo en /vigilados).")
            return
        quitado = next((v for v in lista if v["id"] == numero), None)
        if not quitado:
            self.tg.enviar_texto("Ese número no está en /vigilados.")
            return
        lista.remove(quitado)
        self.tg.enviar_texto(f"✅ Dejé de vigilar «{html.escape(quitado['q'])}».")

    def silencio(self, texto):
        try:
            horas = float((texto.split() or ["2"])[0].replace(",", "."))
        except ValueError:
            horas = 2
        if not math.isfinite(horas):
            horas = 2
        horas = min(max(horas, 0.25), 72)
        hasta = time.time() + horas * 3600
        self.est["silencio_hasta"] = int(hasta)
        self.tg.enviar_texto(f"🔕 Alertas sin sonido hasta las "
                             f"{datetime.fromtimestamp(hasta, LIMA).strftime('%H:%M')} "
                             "(siguen llegando). /sonido para reactivarlo.")

    def sonido(self, _=""):
        self.est.pop("silencio_hasta", None)
        self.tg.enviar_texto("🔔 Sonido activado otra vez (el descanso nocturno sigue igual).")
