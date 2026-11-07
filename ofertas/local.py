"""Modo --local: la PC del usuario revisa las tiendas que bloquean a la nube
(marcadas "solo_local") y vigila que la nube siga funcionando."""

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

import httpx

import config
from ofertas import estado
from ofertas.red import SIN_VENTANA
from ofertas.registro import log
from ofertas.telegram import ErrorTelegram

DIR_BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# El token se lee de este archivo (excluido de git): 1.ª línea token, 2.ª chat id.
ARCHIVO_TOKEN = os.path.join(DIR_BOT, "telegram_token.txt")
NUBE_PARADA_SEGUNDOS = 20 * 60
TAMANO_MAX_REGISTRO = 1_000_000
_ACTIVAS = ("queued", "in_progress", "waiting", "pending", "requested")


def preparar():
    """Estado propio de esta PC y, sin consola (tarea programada), registro en
    datos/local.log. Devuelve la ruta del estado local."""
    carpeta = os.path.join(DIR_BOT, "datos")
    os.makedirs(carpeta, exist_ok=True)
    if sys.stdout is None:
        registro = os.path.join(carpeta, "local.log")
        if os.path.exists(registro) and os.path.getsize(registro) > TAMANO_MAX_REGISTRO:
            os.replace(registro, registro + ".1")
        sys.stdout = sys.stderr = open(registro, "a", encoding="utf-8", buffering=1)
    return os.path.join(carpeta, "estado_local.json.gz")


def leer_token():
    """Token (1.ª línea) y, opcional, chat id (2.ª línea)."""
    try:
        with open(ARCHIVO_TOKEN, encoding="utf-8-sig") as f:
            lineas = [linea.strip() for linea in f.read().splitlines() if linea.strip()]
    except OSError:
        return "", ""
    return (lineas[0] if lineas else ""), (lineas[1] if len(lineas) > 1 else "")


def _openssl():
    ruta = shutil.which("openssl") or r"C:\Program Files\Git\usr\bin\openssl.exe"
    return ruta if os.path.exists(ruta) else None


def chat_de_la_nube(token, est):
    """Toma el chat vinculado del historial que guarda la nube (rama pública
    "estado", cifrada con el token del bot)."""
    repo, openssl = config.PRIVADA.get("repo_nube"), _openssl()
    if not repo or not openssl:
        return None
    try:
        r = httpx.get(f"https://raw.githubusercontent.com/{repo}/estado/estado.enc", timeout=30)
        r.raise_for_status()
    except httpx.HTTPError:
        return None
    chat = None
    with tempfile.TemporaryDirectory() as carpeta:
        cifrado, plano = os.path.join(carpeta, "e.enc"), os.path.join(carpeta, "e.json.gz")
        with open(cifrado, "wb") as f:
            f.write(r.content)
        for clave in (token, token + "\n"):  # por si el Secret se guardó con salto de línea
            try:
                res = subprocess.run(
                    [openssl, "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                     "-pass", "env:CLAVE_ESTADO", "-in", cifrado, "-out", plano],
                    env={**os.environ, "CLAVE_ESTADO": clave}, capture_output=True,
                    timeout=60, creationflags=SIN_VENTANA)
            except (OSError, subprocess.TimeoutExpired):
                return None
            if res.returncode == 0:
                chat = estado.cargar(plano).get("dueno_chat")
                if chat:
                    break
    if chat:
        est["dueno_chat"] = chat
    return chat or None


def _fecha(texto):
    try:
        return datetime.fromisoformat(str(texto).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def vigilar_nube(tg, est, ahora):
    """Si la nube lleva más de 20 min sin revisar (GitHub perdió la cadena), avisa
    una vez por Telegram con un botón para reanudarla. Usa la consulta pública
    de GitHub: no necesita credenciales de GitHub en la PC."""
    repo = config.PRIVADA.get("repo_nube")
    if not repo:
        return
    try:
        r = httpx.get(f"https://api.github.com/repos/{repo}/actions/workflows/bot.yml/runs",
                      params={"per_page": 5}, timeout=30,
                      headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        ejecuciones = r.json().get("workflow_runs") or []
    except (httpx.HTTPError, ValueError, AttributeError):
        return
    if not isinstance(ejecuciones, list):
        return
    ejecuciones = [e for e in ejecuciones if isinstance(e, dict)]
    fechas = [f for e in ejecuciones if (f := _fecha(e.get("created_at"))) is not None]
    if not fechas:
        return  # sin datos fiables: mejor no avisar en falso
    activa = any(e.get("status") in _ACTIVAS for e in ejecuciones)
    parada = not activa and ahora - max(fechas) > NUBE_PARADA_SEGUNDOS
    if parada and not est.get("aviso_nube"):
        try:
            tg.enviar_texto(
                "⚠️ La revisión en la nube se detuvo (GitHub no lanzó la siguiente). "
                "Pulsa el botón, luego <b>Run workflow</b> y otra vez <b>Run workflow</b> "
                "para reanudarla.",
                botones={"inline_keyboard": [[{"text": "▶️ Reanudar en GitHub",
                                               "url": f"https://github.com/{repo}/actions/workflows/bot.yml"}]]})
            est["aviso_nube"] = int(ahora)
            log("La nube estaba detenida: se avisó por Telegram.")
        except ErrorTelegram as e:
            log(f"Telegram: {e}")
    elif not parada:
        est.pop("aviso_nube", None)
