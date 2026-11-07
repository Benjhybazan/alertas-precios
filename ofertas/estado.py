"""Estado persistente (historial de precios, avisos enviados, salud de tiendas).

Se guarda como JSON comprimido. En GitHub Actions el archivo vive en la rama
"estado" del propio repositorio (cifrado); en local, en datos/estado_local.json.gz."""

import gzip
import json
import os
import zlib

import config

DIA = 86400
VERSION = 2
# Nivel de compresión: 6 comprime casi igual que 9 y es varias veces más rápido.
COMPRESION = 6


def nuevo() -> dict:
    return {
        "version": VERSION,
        "productos": {},        # "tienda:id" -> {h, v, a, pend}
        "fuentes": {},          # id de fuente -> última vez leída (línea base)
        "cursores": {},         # tienda -> cursor propio (para tiendas que lean por turnos)
        "inicios": {},          # tienda -> por qué fuente empezar (límite de tiempo)
        "uso_mes": {},          # minutos de GitHub estimados del mes (repo privado)
        "avisos_recientes": {}, # nombre+precio -> [ts, producto] (evita repetir entre tiendas)
        "salud": {},            # tienda -> {fallos, error, desde, avisado, ok}
        "ejecuciones": [],      # [ts, productos, alertas, tiendas_ok, tiendas_total]
        "telegram_offset": 0,
        "dueno_chat": "",       # chat vinculado con /start (si no hay TELEGRAM_CHAT_ID)
        "resumen_diario": "",
    }


def _migrar(est: dict):
    """Versión 1 -> 2: se quitan datos que nunca se leían (nombre del producto y
    precio anterior de cada tramo del historial); el estado ocupa ~40 % menos."""
    if est.get("version", 1) >= VERSION:
        return
    for p in est["productos"].values():
        if isinstance(p, dict):
            p.pop("n", None)
            h = p.get("h")
            if isinstance(h, list):
                p["h"] = [t[:2] for t in h if isinstance(t, list) and len(t) >= 2]
    est["version"] = VERSION


def cargar(ruta: str) -> dict:
    if not os.path.exists(ruta):
        return nuevo()
    try:
        with gzip.open(ruta, "rt", encoding="utf-8") as f:
            datos = json.load(f)
    except (OSError, EOFError, zlib.error, ValueError) as e:
        print(f"[estado] No se pudo leer el estado ({e.__class__.__name__}); se empieza de cero.")
        return nuevo()
    if not isinstance(datos, dict):
        print("[estado] El estado no tiene el formato esperado; se empieza de cero.")
        return nuevo()
    base = nuevo()
    for clave, valor in datos.items():
        # Un campo con un tipo inesperado (archivo dañado o editado) se descarta
        # en vez de romper la ejecución más adelante.
        if clave in base and isinstance(base[clave], (dict, list, int, str)) \
                and not isinstance(valor, type(base[clave])):
            continue
        base[clave] = valor
    if "version" not in datos:
        base["version"] = 1
    _migrar(base)
    return base


def guardar(est: dict, ruta: str):
    """Escritura atómica: se escribe un temporal y se reemplaza el archivo, así un
    corte a mitad de camino nunca deja el estado a medias."""
    os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
    temporal = ruta + ".tmp"
    with gzip.open(temporal, "wt", encoding="utf-8", compresslevel=COMPRESION) as f:
        json.dump(est, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(temporal, ruta)


def podar(est: dict, ahora: float):
    limite = ahora - config.DIAS_RETENCION * DIA
    est["productos"] = {k: v for k, v in est["productos"].items()
                        if isinstance(v, dict) and v.get("v", 0) >= limite}
    # Una fuente que no se lee hace 3 días vuelve a empezar con línea base.
    est["fuentes"] = {k: t for k, t in est["fuentes"].items()
                      if isinstance(t, (int, float)) and t >= ahora - 3 * DIA}
    est["avisos_recientes"] = {k: v for k, v in est["avisos_recientes"].items()
                               if isinstance(v, list) and v and v[0] >= ahora - DIA}
    est["ejecuciones"] = [e for e in est["ejecuciones"]
                          if isinstance(e, list) and e and e[0] >= ahora - DIA]
