"""Bot de Telegram detector de ofertas extremas y errores de precio en Perú.

Cada ejecución revisa las tiendas una vez, envía las alertas nuevas y guarda
el estado. En la nube lo ejecuta GitHub Actions cada pocos minutos.

Uso:
  python bot.py                    revisión normal (necesita TELEGRAM_TOKEN)
  python bot.py --sin-telegram     prueba: muestra las alertas en pantalla, sin enviar ni guardar
  python bot.py --chat-id          muestra el chat id de quien escribió al bot
  python bot.py --probar-telegram  envía un mensaje de prueba con botón
  python bot.py --solo CLAVE       revisa solo una tienda (su clave en la configuración)
  python bot.py --local            revisa en esta PC las tiendas marcadas "solo_local"
                                   (lo usa la tarea programada de Windows)

Módulos (carpeta ofertas/): descarga (tiendas en paralelo), analisis (qué es
oferta real), alertas (qué avisar y cómo), comandos (Telegram interactivo),
informes (salud y /estado), local (modo PC), estado (historial persistente).
"""

import argparse
import os
import sys
import time
import traceback

import config
from ofertas import comandos, estado, local, registro
from ofertas.alertas import alertas_vigiladas, elegir_alertas, enviar_alertas
from ofertas.descarga import descargar, unificar
from ofertas.informes import actualizar_salud, controlar_minutos, resumen_diario, texto_estado
from ofertas.registro import log
from ofertas.telegram import ErrorTelegram, Telegram
from ofertas.tiendas import crear_tiendas

RUTA_ESTADO = os.environ.get("RUTA_ESTADO", os.path.join("datos", "estado.json.gz"))

# Repositorio público: tiempo máximo para enviar alertas; lo que no alcance
# queda pendiente para la siguiente ejecución (GitHub corta el trabajo a los 6 min).
PRESUPUESTO_SEGUNDOS = 4 * 60


def _entero(nombre, defecto=0):
    try:
        return int(os.environ.get(nombre) or defecto)
    except ValueError:
        return defecto


# En la nube, tras revisar, el bot sigue escuchando comandos hasta estos segundos
# desde el inicio (lo fija el flujo de GitHub); 0 = solo mientras revisa.
ESCUCHAR_SEGUNDOS = _entero("ESCUCHAR_SEGUNDOS")


def _repo_privado():
    """True / False según GitHub; None si no se sabe (o fuera de GitHub)."""
    valor = os.environ.get("REPO_PRIVADO", "").strip().lower()
    return {"true": True, "false": False}.get(valor)


def _discreto(privado):
    """En GitHub, registros discretos salvo que conste que el repo es privado:
    ante la duda se protege la receta."""
    return privado is False or (privado is None and bool(os.environ.get("GITHUB_ACTIONS")))


# ---------------------------------------------------------------------------
# Ejecución principal
# ---------------------------------------------------------------------------

def preparar_telegram(args, est):
    """Devuelve (tg, token, chat_id, codigo_salida). codigo_salida no es None
    cuando la ejecución debe terminar ya (falta configuración o vincular el chat)."""
    token = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if args.local and not token:
        token, chat_id = local.leer_token()
        if not token:
            log("Falta el token: pégalo en telegram_token.txt (carpeta del bot).")
            return None, "", "", 1
    if args.sin_telegram:
        return None, token, chat_id, None
    if args.local and token and not chat_id:
        # El chat vinculado está en el historial cifrado de la nube.
        chat_id = est.get("dueno_chat") or local.chat_de_la_nube(token, est)
        if not chat_id:
            log("No se encontró el chat vinculado. Escribe /start al bot y espera la "
                "siguiente revisión de la nube, o pon tu chat id en la 2.ª línea de "
                "telegram_token.txt.")
            return None, token, "", 1
    if token and not chat_id:
        # Sin TELEGRAM_CHAT_ID: se usa el chat que se vinculó con /start.
        chat_id = est.get("dueno_chat") or vincular_dueno(token, est)
        if not chat_id:
            log("Esperando a que escribas /start a tu bot en Telegram para vincular tu chat.")
            estado.guardar(est, RUTA_ESTADO)
            return None, token, "", 0
    if token and chat_id:
        return Telegram(token, chat_id), token, chat_id, None
    if os.environ.get("GITHUB_ACTIONS"):
        log("ERROR: falta el Secret TELEGRAM_TOKEN del repositorio.")
        return None, token, chat_id, 1
    log("Falta TELEGRAM_TOKEN: modo prueba (solo se muestran las alertas).")
    return None, token, chat_id, None


def elegir_tiendas(args):
    tiendas, invalidas = crear_tiendas(config.TIENDAS)
    for clave, motivo in invalidas:
        log(f"Tienda {clave} omitida: {motivo}", detalle=True)
    if invalidas:
        log(f"{len(invalidas)} tienda(s) con configuración inválida: se omiten.")
    # "solo_local": tiendas que bloquean a los servidores en la nube y se revisan
    # desde la PC del usuario (modo --local); la nube no las intenta.
    if args.local:
        tiendas = [t for t in tiendas if t.cfg.get("solo_local")]
    elif os.environ.get("GITHUB_ACTIONS"):
        tiendas = [t for t in tiendas if not t.cfg.get("solo_local")]
    if args.solo:
        tiendas = [t for t in tiendas if t.clave == args.solo]
    return tiendas


def ejecutar(args):
    ahora, inicio = time.time(), time.monotonic()
    privado = _repo_privado()
    registro.discreto = _discreto(privado)
    if not config.TIENDAS:
        log("ERROR: no hay configuración privada (Secret CONFIGURACION en GitHub, o "
            "config_privada.json en tu PC).")
        return 1
    est = estado.cargar(RUTA_ESTADO)
    tg, token, chat_id, salida = preparar_telegram(args, est)
    if salida is not None:
        return salida
    # En modo prueba no se guarda nada, para no dar por avisadas alertas no enviadas.
    guardar = tg is not None
    if args.local and tg:
        local.vigilar_nube(tg, est, ahora)
    # Repositorio privado: 2.000 minutos gratis al mes y GitHub cobra cada
    # ejecución redondeando al minuto, así que cada ejecución dura < 1 minuto.
    if privado:
        limite_descarga = inicio + config.SEGUNDOS_DESCARGA_PRIVADO
        limite_total = inicio + config.SEGUNDOS_TOTAL_PRIVADO
    else:
        limite_descarga, limite_total = None, inicio + PRESUPUESTO_SEGUNDOS

    tiendas = elegir_tiendas(args)
    nombres = {t.clave: t.nombre for t in tiendas}
    # Tiendas que bloquearon al bot: no se insiste hasta que termine su pausa.
    en_pausa = [t for t in tiendas if est["salud"].get(t.clave, {}).get("pausa_hasta", 0) > ahora]
    activas = [t for t in tiendas if t not in en_pausa]
    tiendas_ok = 0
    escucha = None
    if tg and not args.local:  # los comandos los atiende solo la nube
        escucha = comandos.Escucha(Telegram(token, chat_id), est, nombres, texto_estado)
        escucha.start()
    try:
        resultados = descargar(activas, est, limite_descarga)
        log(f"Descarga terminada en {time.monotonic() - inicio:.0f} s")
        productos, linea_base = unificar(resultados, est, nombres, ahora)
        con_datos = sum(1 for r in resultados.values() if r["lecturas"])
        con_errores = sum(1 for r in resultados.values() if r["errores"])
        log(f"Tiendas con datos: {con_datos} de {len(resultados)}"
            + (f" · {con_errores} con errores (detalle: /estado en Telegram)"
               if con_errores and registro.discreto else "")
            + (f" · {len(en_pausa)} en pausa por bloqueo" if en_pausa else ""))
        evaluaciones = {}
        alertas = elegir_alertas(productos, linea_base, est, ahora, marcar_pendientes=guardar,
                                 evaluaciones=evaluaciones)
        alertas += alertas_vigiladas(productos, evaluaciones, est, {p.clave for p, _ in alertas})
        if escucha:
            escucha.publicar_revision(productos, evaluaciones)
        log(f"{len(productos)} productos analizados, {len(alertas)} alertas nuevas")

        enviadas = enviar_alertas(tg, alertas, nombres, est, ahora, limite=limite_total)
        tiendas_ok = actualizar_salud(tg, est, resultados, nombres, ahora)
        est["ejecuciones"].append([int(ahora), len(productos), enviadas, tiendas_ok, len(tiendas)])
        if escucha:  # el resumen y los comandos son cosa de la nube
            try:
                resumen_diario(tg, est, nombres, ahora)
            except ErrorTelegram as e:
                log(f"Telegram: {e}")
            escucha.terminar(inicio + ESCUCHAR_SEGUNDOS)
        if privado:
            controlar_minutos(tg, est, ahora, time.monotonic() - inicio)
    finally:
        # Pase lo que pase, el historial y las alertas ya enviadas se guardan.
        if escucha:
            escucha.terminar()
            escucha.tg.cerrar()
        if tg:
            tg.cerrar()
        if guardar:
            estado.podar(est, ahora)
            estado.guardar(est, RUTA_ESTADO)
            log(f"Estado guardado ({len(est['productos'])} productos en historial)")
    # Si ninguna tienda revisada respondió, la ejecución se marca como fallida en GitHub.
    return 0 if tiendas_ok or not activas else 1


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
                                "de ofertas.\n\n/menu — botones de acceso rápido · /ayuda — comandos")
                log("Chat de Telegram vinculado.")
                return est["dueno_chat"]
    except ErrorTelegram as e:
        log(f"Telegram: {e}")
    finally:
        tg.cerrar()
    return None


# ---------------------------------------------------------------------------
# Utilidades de configuración
# ---------------------------------------------------------------------------

def mostrar_chat_id():
    token = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
    if not token:
        sys.exit("Define primero la variable TELEGRAM_TOKEN con el token de @BotFather.")
    tg = Telegram(token, "0")
    vistos = {}
    try:
        for upd in tg.actualizaciones(0):
            chat = (upd.get("message") or {}).get("chat") or {}
            if chat.get("id"):
                vistos[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    finally:
        tg.cerrar()
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
    try:
        tg.enviar_texto("✅ Prueba correcta: el bot de ofertas puede escribirte.",
                        botones={"inline_keyboard": [[{"text": "🛒 COMPRAR AHORA",
                                                       "url": "https://telegram.org/"}]]})
    finally:
        tg.cerrar()
    print("Mensaje de prueba enviado. Revisa Telegram.")


def main():
    global RUTA_ESTADO
    parser = argparse.ArgumentParser(description="Bot de ofertas extremas (Perú)")
    parser.add_argument("--sin-telegram", action="store_true", help="no envía nada, solo muestra")
    parser.add_argument("--chat-id", action="store_true", help="muestra tu TELEGRAM_CHAT_ID")
    parser.add_argument("--probar-telegram", action="store_true", help="envía un mensaje de prueba")
    parser.add_argument("--solo", help="revisa solo esta tienda (su clave en la configuración)")
    parser.add_argument("--local", action="store_true",
                        help='revisa en esta PC las tiendas marcadas "solo_local"')
    args = parser.parse_args()
    registro.discreto = _discreto(_repo_privado())
    if args.local:
        RUTA_ESTADO = local.preparar()
    if sys.stdout and sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.chat_id:
        return mostrar_chat_id()
    if args.probar_telegram:
        return probar_telegram()
    try:
        codigo = ejecutar(args)
    except Exception as e:
        # Fallo imprevisto: queda en el registro (en la PC, datos/local.log) en vez
        # de perderse; en el repositorio público, sin detalles que revelen la receta.
        log(f"ERROR inesperado: {e.__class__.__name__}")
        log(traceback.format_exc(), detalle=True)
        codigo = 1
    sys.exit(codigo)


if __name__ == "__main__":
    main()
