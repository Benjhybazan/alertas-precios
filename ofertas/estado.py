"""Estado persistente (historial de precios, avisos enviados, salud de tiendas).

Se guarda como JSON comprimido. En GitHub Actions el archivo vive en la rama
"estado" del propio repositorio; en local, en datos/estado.json.gz."""

import gzip
import json
import os
import zlib

import config

DIA = 86400


def nuevo() -> dict:
    return {
        "version": 1,
        "productos": {},        # "tienda:id" -> {n, h, v, a, pend}
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


def cargar(ruta: str) -> dict:
    if not os.path.exists(ruta):
        return nuevo()
    try:
        with gzip.open(ruta, "rt", encoding="utf-8") as f:
            datos = json.load(f)
    except (OSError, EOFError, zlib.error, ValueError) as e:
        print(f"[estado] No se pudo leer {ruta} ({e}); se empieza de cero.")
        return nuevo()
    if not isinstance(datos, dict):
        print(f"[estado] {ruta} no tiene el formato esperado; se empieza de cero.")
        return nuevo()
    base = nuevo()
    base.update(datos)
    return base


def guardar(est: dict, ruta: str):
    os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
    temporal = ruta + ".tmp"
    with gzip.open(temporal, "wt", encoding="utf-8") as f:
        json.dump(est, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(temporal, ruta)


def podar(est: dict, ahora: float):
    limite = ahora - config.DIAS_RETENCION * DIA
    est["productos"] = {k: v for k, v in est["productos"].items() if v.get("v", 0) >= limite}
    # Una fuente que no se lee hace 3 días vuelve a empezar con línea base.
    est["fuentes"] = {k: t for k, t in est["fuentes"].items() if t >= ahora - 3 * DIA}
    est["avisos_recientes"] = {k: v for k, v in est["avisos_recientes"].items()
                               if isinstance(v, list) and v[0] >= ahora - DIA}
    est["ejecuciones"] = [e for e in est["ejecuciones"] if e[0] >= ahora - DIA]
