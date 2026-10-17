"""Bot de Telegram detector de ofertas extremas y errores de precio en Perú.

Cada ejecución revisa las tiendas una vez, envía las alertas nuevas y guarda
el estado. En la nube lo ejecuta GitHub Actions cada pocos minutos.

Uso:
  python bot.py                    revisión normal (necesita TELEGRAM_TOKEN y TELEGRAM_CHAT_ID)
  python bot.py --sin-telegram     prueba: muestra las alertas en pantalla, sin enviar ni guardar
  python bot.py --chat-id          muestra el chat id de quien escribió al bot
  python bot.py --probar-telegram  envía un mensaje de prueba con botón
  python bot.py --solo CLAVE       revisa solo una tienda (su clave en la configuración)
"""

import argparse
import html
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import config
from ofertas import analisis, estado
from ofertas.red import Descargador, ErrorDescarga
from ofertas.telegram import LIMA, ErrorTelegram, Telegram, hora_lima, soles, texto_alerta
from ofertas.tiendas import crear_tiendas
from ofertas.tiendas.base import slug

RUTA_ESTADO = os.environ.get("RUTA_ESTADO", os.path.join("datos", "estado.json.gz"))

# Repositorio público: tiempo máximo para enviar alertas; lo que no alcance
# queda pendiente para la siguiente ejecución (GitHub corta el trabajo a los 6 min).
PRESUPUESTO_SEGUNDOS = 4 * 60
# Segundos que GitHub tarda en preparar y cerrar cada ejecución (fuera del bot).
SEGUNDOS_PREPARACION = 15
# Las mejores alertas se envían siempre, aunque se haya acabado el tiempo.
ALERTAS_GARANTIZADAS = 3


# Repositorio público: los registros de GitHub los puede ver cualquiera, así que
# solo muestran totales (sin tiendas, categorías ni errores detallados). Los
# detalles se consultan en Telegram con /estado.
DISCRETO = False


def log(msg, detalle=False):
    if detalle and DISCRETO:
        return
    print(f"[{hora_lima()}] {msg}", flush=True)


def sin_html(texto):
    """Versión para consola de un mensaje con formato HTML de Telegram."""
    return html.unescape(re.sub(r"<[^>]+>", "", texto))


def firma(p):
    """Mismo artículo al mismo precio, aunque lo publiquen varias tiendas."""
    return f"{slug(p.nombre)[:60]}|{round(p.precio)}"


# ---------------------------------------------------------------------------
# Descarga
# ---------------------------------------------------------------------------

def revisar_tienda(tienda, cursor, inicio, limite):
    """Lee las fuentes de una tienda empezando por la posición `inicio`.

    Si hay límite de tiempo (repositorio privado) y no alcanza, la siguiente
    ejecución continúa por las fuentes que quedaron sin leer."""
    fuentes, nuevo_cursor = tienda.fuentes(cursor)
    k = inicio % len(fuentes) if fuentes else 0
    cola, tomadas, candado = iter(fuentes[k:] + fuentes[:k]), [], threading.Lock()

    def siguiente():
        with candado:
            if limite and time.monotonic() > limite:
                return None
            f = next(cola, None)
            if f:
                tomadas.append(f)
            return f

    n = max(1, min(tienda.conexiones, len(fuentes)))
    with ThreadPoolExecutor(max_workers=n) as pool:
        partes = list(pool.map(lambda _: leer_fuentes(tienda, siguiente, limite), range(n)))
    return {
        "lecturas": [x for lec, _ in partes for x in lec],
        "errores": [x for _, err in partes for x in err],
        "cursor": nuevo_cursor,
        "inicio": (k + len(tomadas)) % len(fuentes) if fuentes else 0,
        "sin_leer": len(fuentes) - len(tomadas),
    }


def leer_fuentes(tienda, siguiente, limite=None):
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
                if e.codigo in (403, 429):
                    break  # la tienda nos frena: no insistir en esta ejecución
            except Exception as e:  # cambio en la web: no debe tumbar la ejecución
                errores.append(f"{f.id}: formato inesperado ({e.__class__.__name__}: {e})")
    finally:
        d.cerrar()
    return lecturas, errores


# ---------------------------------------------------------------------------
# Ejecución principal
# ---------------------------------------------------------------------------

def ejecutar(args):
    global DISCRETO
    ahora, inicio = time.time(), time.monotonic()
    DISCRETO = os.environ.get("REPO_PRIVADO", "").strip().lower() == "false"
    if not config.TIENDAS:
        log("ERROR: no hay configuración privada (Secret CONFIGURACION en GitHub, o "
            "config_privada.json en tu PC).")
        return 1
    token = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    est = estado.cargar(RUTA_ESTADO)
    tg = None
    if not args.sin_telegram:
        if token and not chat_id:
            # Sin TELEGRAM_CHAT_ID: se usa el chat que se vinculó con /start.
            chat_id = est.get("dueno_chat") or vincular_dueno(token, est)
            if not chat_id:
                log("Esperando a que escribas /start a tu bot en Telegram para vincular tu chat.")
                estado.guardar(est, RUTA_ESTADO)
                return 0
        if token and chat_id:
            tg = Telegram(token, chat_id)
        elif os.environ.get("GITHUB_ACTIONS"):
            log("ERROR: falta el Secret TELEGRAM_TOKEN del repositorio.")
            return 1
        else:
            log("Falta TELEGRAM_TOKEN: modo prueba (solo se muestran las alertas).")
    # En modo prueba no se guarda nada, para no dar por avisadas alertas no enviadas.
    guardar = tg is not None
    # Repositorio privado: 2.000 minutos gratis al mes y GitHub cobra cada
    # ejecución redondeando al minuto, así que cada ejecución dura < 1 minuto.
    privado = os.environ.get("REPO_PRIVADO", "").strip().lower() == "true"
    if privado:
        limite_descarga = inicio + config.SEGUNDOS_DESCARGA_PRIVADO
        limite_total = inicio + config.SEGUNDOS_TOTAL_PRIVADO
    else:
        limite_descarga, limite_total = None, inicio + PRESUPUESTO_SEGUNDOS

    tiendas = crear_tiendas(config.TIENDAS)
    if args.solo:
        tiendas = [t for t in tiendas if t.clave == args.solo]
    nombres = {t.clave: t.nombre for t in tiendas}
    tiendas_ok = 0
    try:
        resultados = descargar(tiendas, est, limite_descarga)
        log(f"Descarga terminada en {time.monotonic() - inicio:.0f} s")
        productos, linea_base = unificar(resultados, est, nombres, ahora)
        con_datos = sum(1 for r in resultados.values() if r["lecturas"])
        con_errores = sum(1 for r in resultados.values() if r["errores"])
        log(f"Tiendas con datos: {con_datos} de {len(resultados)}"
            + (f" · {con_errores} con errores (detalle: /estado en Telegram)"
               if con_errores and DISCRETO else ""))
        alertas = elegir_alertas(productos, linea_base, est, ahora, marcar_pendientes=guardar)
        log(f"{len(productos)} productos analizados, {len(alertas)} alertas nuevas")

        enviadas = enviar_alertas(tg, alertas, nombres, est, ahora, limite=limite_total)
        tiendas_ok = actualizar_salud(tg, est, resultados, nombres, ahora)
        est["ejecuciones"].append([int(ahora), len(productos), enviadas, tiendas_ok, len(tiendas)])
        if tg:
            try:
                resumen_diario(tg, est, nombres, ahora)
                atender_comandos(tg, est, nombres, ahora)
            except ErrorTelegram as e:
                log(f"Telegram: {e}")
        if privado:
            controlar_minutos(tg, est, ahora, time.monotonic() - inicio)
    finally:
        # Pase lo que pase, el historial y las alertas ya enviadas se guardan.
        if tg:
            tg.cerrar()
        if guardar:
            estado.podar(est, ahora)
            estado.guardar(est, RUTA_ESTADO)
            log(f"Estado guardado ({len(est['productos'])} productos en historial)")
    # Si ninguna tienda respondió, la ejecución se marca como fallida en GitHub.
    return 0 if tiendas_ok or not tiendas else 1


def vincular_dueno(token, est):
    """Sin TELEGRAM_CHAT_ID, el primer chat privado que escribe /start al bot
    queda como destinatario de las alertas (se guarda en el historial cifrado).
    Si se define el Secret TELEGRAM_CHAT_ID, ese tiene prioridad."""
    tg = Telegram(token, "0")
    try:
        for upd in tg.actualizaciones(est.get("telegram_offset", 0)):
            est["telegram_offset"] = upd["update_id"] + 1
            msg = upd.get("message") or {}
            chat = msg.get("chat") or {}
            if chat.get("type") == "private" and (msg.get("text") or "").startswith("/start"):
                est["dueno_chat"] = str(chat["id"])
                tg.chat_id = est["dueno_chat"]
                tg.enviar_texto("✅ ¡Listo! Este chat quedó vinculado: aquí recibirás las alertas "
                                "de ofertas.\n\n/estado — ver si el bot y las tiendas funcionan")
                log("Chat de Telegram vinculado.")
                return est["dueno_chat"]
    except ErrorTelegram as e:
        log(f"Telegram: {e}")
    finally:
        tg.cerrar()
    return None


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


def unificar(resultados, est, nombres, ahora):
    """Un producto puede salir en varias fuentes: se cuenta una sola vez.

    linea_base[clave] es True si TODAS sus fuentes se leen por primera vez."""
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
                if p.clave not in productos:
                    productos[p.clave] = p
                    linea_base[p.clave] = nueva
                else:
                    linea_base[p.clave] = linea_base[p.clave] and nueva
            est["fuentes"][fuente.id] = int(ahora)
    return productos, linea_base


def elegir_alertas(productos, linea_base, est, ahora, marcar_pendientes):
    """Evalúa cada producto, registra su historial y devuelve las alertas a enviar."""
    candidatas = []
    for clave, p in productos.items():
        est_p = est["productos"].get(clave)
        ev = analisis.evaluar(p, (est_p or {}).get("h"), ahora)
        if ev and analisis.es_novedad(p, ev, est_p, linea_base[clave]):
            candidatas.append((p, ev))
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


def marcar_avisado(est, p, ev, ahora):
    est_p = est["productos"][p.clave]
    est_p["a"] = {"t": int(ahora), "p": p.precio, "n": ev.nivel}
    est_p.pop("pend", None)
    est["avisos_recientes"][firma(p)] = [int(ahora), p.clave]


def enviar_alertas(tg, alertas, nombres, est, ahora, limite):
    """Las mejores alertas van con foto y botón; el resto, en un mensaje resumen.
    Las que no se logren enviar quedan pendientes ("pend") para la siguiente vez."""
    individuales = alertas[:config.MAX_ALERTAS_POR_EJECUCION]
    resto = alertas[config.MAX_ALERTAS_POR_EJECUCION:]
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
            tg.enviar_alerta(p, ev, nombres[p.tienda])
            marcar_avisado(est, p, ev, ahora)
            enviadas += 1
        except ErrorTelegram as e:
            log(f"No se pudo enviar la alerta de {p.clave}: {e}", detalle=True)
            if e.red:
                return enviadas  # Telegram no responde: no insistir con el resto
    if not resto:
        return enviadas
    lineas = [f"{ev.titulo.split()[0]} <b>-{ev.ahorro_pct:.0%}</b> {soles(p.precio)} · "
              f"<a href=\"{html.escape(p.url)}\">{html.escape(p.nombre[:70])}</a> "
              f"({nombres[p.tienda]})"
              for p, ev in resto]
    encabezado = f"➕ <b>Otras {len(resto)} ofertas detectadas en esta revisión:</b>"
    if tg is None:
        print(sin_html(encabezado + "\n" + "\n".join(lineas)))
        return enviadas + len(resto)
    if time.monotonic() > limite:
        return enviadas
    try:
        tg.enviar_lista(encabezado, lineas)
    except ErrorTelegram as e:
        log(f"No se pudo enviar el resumen: {e}")
        return enviadas
    for p, ev in resto:
        marcar_avisado(est, p, ev, ahora)
    return enviadas + len(resto)


def actualizar_salud(tg, est, resultados, nombres, ahora):
    ok = 0
    for clave, r in resultados.items():
        lecturas, errores = r["lecturas"], r["errores"]
        if not lecturas and not errores:
            continue  # no se alcanzó a consultar (límite de tiempo): ni éxito ni fallo
        s = est["salud"].setdefault(clave, {"fallos": 0})
        if lecturas:
            ok += 1
            if s.get("avisado") and tg:
                avisar(tg, f"✅ {nombres[clave]} vuelve a responder con normalidad.")
            est["salud"][clave] = {"fallos": 0, "ok": int(ahora), "errores": len(errores)}
            continue
        s["fallos"] = s.get("fallos", 0) + 1
        s["error"] = (errores or ["sin datos"])[0][:200]
        s.setdefault("desde", int(ahora))
        if s["fallos"] >= config.FALLOS_PARA_AVISAR and not s.get("avisado") and tg:
            avisar(tg, f"⚠️ <b>{nombres[clave]}</b> no responde desde las {hora_lima(s['desde'])} "
                       f"({s['fallos']} revisiones seguidas).\nÚltimo error: {html.escape(s['error'])}")
            s["avisado"] = True
    return ok


def controlar_minutos(tg, est, ahora, segundos):
    """Estima los minutos gratis de GitHub usados en el mes (repositorio privado)
    y avisa una vez si se acercan al límite. El dato exacto está en GitHub:
    Settings → Billing and plans."""
    mes = datetime.fromtimestamp(ahora, timezone.utc).strftime("%Y-%m")
    uso = est.get("uso_mes") or {}
    if uso.get("mes") != mes:
        uso = {"mes": mes, "min": 0}
    uso["min"] += math.ceil((segundos + SEGUNDOS_PREPARACION) / 60)
    if uso["min"] >= 0.85 * config.MINUTOS_GRATIS_MES and not uso.get("avisado") and tg:
        avisar(tg, f"⚠️ El bot lleva ~{uso['min']:,} de {config.MINUTOS_GRATIS_MES:,} minutos "
                   "gratis de GitHub este mes. Si se agotan, se detendrá hasta el día 1 del mes "
                   "siguiente (sin cobrarte). Revisa GitHub → Settings → Billing and plans.")
        uso["avisado"] = True
    est["uso_mes"] = uso


def avisar(tg, texto):
    try:
        tg.enviar_texto(texto)
    except ErrorTelegram as e:
        log(f"Telegram: {e}")


def texto_estado(est, nombres, ahora):
    ej = est["ejecuciones"]
    if ej:
        ultima = ej[-1][0]
        lineas = [f"✅ <b>Bot activo</b>. Última revisión: {hora_lima(ultima)} "
                  f"(hace {int((ahora - ultima) // 60)} min)"]
    else:
        lineas = ["ℹ️ El bot aún no ha completado ninguna revisión."]
    lineas.append(f"Últimas 24 h: {len(ej)} revisiones · "
                  f"{sum(e[1] for e in ej):,} productos analizados · {sum(e[2] for e in ej)} alertas")
    lineas.append("\n<b>Tiendas</b>")
    for clave, nombre in nombres.items():
        s = est["salud"].get(clave, {})
        if s.get("fallos"):
            lineas.append(f"⚠️ {nombre}: falla desde las {hora_lima(s.get('desde'))} — "
                          f"{html.escape(s.get('error', '')[:80])}")
        elif s.get("ok"):
            lineas.append(f"✅ {nombre}: OK ({hora_lima(s['ok'])})")
        else:
            lineas.append(f"… {nombre}: sin datos todavía")
    uso = est.get("uso_mes")
    if uso:
        lineas.append(f"\n⏱️ Minutos de GitHub este mes (estimado): {uso['min']:,} de "
                      f"{config.MINUTOS_GRATIS_MES:,}")
    lineas.append(f"\nUmbrales: descuento ≥ {config.MIN_DESCUENTO:.0%} · referencia ≥ "
                  f"{soles(config.MIN_PRECIO_REFERENCIA)} · nivel mínimo {config.NIVEL_MINIMO}")
    return "\n".join(lineas)


def resumen_diario(tg, est, nombres, ahora):
    if config.HORA_RESUMEN_DIARIO < 0:
        return
    local = datetime.fromtimestamp(ahora, LIMA)
    hoy = local.strftime("%Y-%m-%d")
    if local.hour >= config.HORA_RESUMEN_DIARIO and est.get("resumen_diario") != hoy:
        tg.enviar_texto("☀️ <b>Resumen diario</b>\n" + texto_estado(est, nombres, ahora))
        est["resumen_diario"] = hoy


def atender_comandos(tg, est, nombres, ahora):
    """Responde /estado y /start. Solo al chat configurado; ignora a extraños."""
    for upd in tg.actualizaciones(est.get("telegram_offset", 0)):
        est["telegram_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != tg.chat_id:
            continue
        comando = (msg.get("text") or "").strip().split("@")[0].lower()
        if comando in ("/estado", "/status"):
            tg.enviar_texto(texto_estado(est, nombres, ahora))
        elif comando in ("/start", "/ayuda", "/help"):
            tg.enviar_texto("🤖 Bot de ofertas extremas activo.\nTe avisaré cuando detecte "
                            "posibles errores de precio y descuentos excepcionales.\n\n"
                            "/estado — ver si el bot y las tiendas funcionan")


# ---------------------------------------------------------------------------
# Utilidades de configuración
# ---------------------------------------------------------------------------

def mostrar_chat_id():
    token = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
    if not token:
        sys.exit("Define primero la variable TELEGRAM_TOKEN con el token de @BotFather.")
    tg = Telegram(token, "0")
    vistos = {}
    for upd in tg.actualizaciones(0):
        chat = (upd.get("message") or {}).get("chat") or {}
        if chat.get("id"):
            vistos[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    if not vistos:
        print("No hay mensajes. Abre tu bot en Telegram, pulsa INICIAR (o escribe /start) "
              "y vuelve a ejecutar este comando.")
    for cid, nombre in vistos.items():
        print(f"TELEGRAM_CHAT_ID = {cid}   ({nombre})")


def probar_telegram():
    token = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        sys.exit("Define TELEGRAM_TOKEN y TELEGRAM_CHAT_ID.")
    tg = Telegram(token, chat_id)
    tg.enviar_texto("✅ Prueba correcta: el bot de ofertas puede escribirte.",
                    botones={"inline_keyboard": [[{"text": "🛒 COMPRAR AHORA",
                                                   "url": "https://telegram.org/"}]]})
    print("Mensaje de prueba enviado. Revisa Telegram.")


def main():
    parser = argparse.ArgumentParser(description="Bot de ofertas extremas (Perú)")
    parser.add_argument("--sin-telegram", action="store_true", help="no envía nada, solo muestra")
    parser.add_argument("--chat-id", action="store_true", help="muestra tu TELEGRAM_CHAT_ID")
    parser.add_argument("--probar-telegram", action="store_true", help="envía un mensaje de prueba")
    parser.add_argument("--solo", help="revisa solo esta tienda (su clave en la configuración)")
    args = parser.parse_args()
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.chat_id:
        return mostrar_chat_id()
    if args.probar_telegram:
        return probar_telegram()
    sys.exit(ejecutar(args))


if __name__ == "__main__":
    main()
