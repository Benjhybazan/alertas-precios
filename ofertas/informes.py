"""Salud de las tiendas, /estado, resumen diario y control de minutos de GitHub."""

import html
import math
import re
from datetime import datetime, timezone

import config
from ofertas.alertas import silenciado
from ofertas.registro import log
from ofertas.telegram import LIMA, ErrorTelegram, hora_lima, soles

# Segundos que GitHub tarda en preparar y cerrar cada ejecución (fuera del bot).
SEGUNDOS_PREPARACION = 15
_RE_BLOQUEO = re.compile(r"HTTP (403|429)")


def avisar(tg, texto):
    try:
        tg.enviar_texto(texto)
    except ErrorTelegram as e:
        log(f"Telegram: {e}")


def actualizar_salud(tg, est, resultados, nombres, ahora):
    """Registra qué tiendas respondieron; avisa una vez si una falla seguido o
    bloquea al bot (y se pausa), y otra vez cuando se recupera."""
    ok = 0
    for clave, r in resultados.items():
        lecturas, errores = r["lecturas"], r["errores"]
        if not lecturas and not errores:
            continue  # no se alcanzó a consultar (límite de tiempo): ni éxito ni fallo
        s = est["salud"].setdefault(clave, {"fallos": 0})
        # Un 403/429 cuenta aunque antes, en la misma revisión, sí diera datos.
        bloqueo = next((m.group(0) for e in errores if (m := _RE_BLOQUEO.search(e))), None)
        if lecturas:
            ok += 1
            if s.get("avisado") and not bloqueo and tg:
                avisar(tg, f"✅ {html.escape(nombres[clave])} vuelve a responder con normalidad.")
            s = {"fallos": 0, "ok": int(ahora), "errores": len(errores)}
            if bloqueo and est["salud"][clave].get("avisado"):
                s["avisado"] = True
            est["salud"][clave] = s
            if not bloqueo:
                continue
        else:
            s["fallos"] = s.get("fallos", 0) + 1
            s["error"] = (errores or ["sin datos"])[0][:200]
            s.setdefault("desde", int(ahora))
        if bloqueo:
            # La tienda rechaza al bot: se respeta y solo se reintenta tras una pausa.
            s["pausa_hasta"] = int(ahora + config.PAUSA_TRAS_BLOQUEO_HORAS * 3600)
            s["bloqueo"] = bloqueo
            if not s.get("avisado") and tg:
                avisar(tg, f"⏸️ <b>{html.escape(nombres[clave])}</b> está bloqueando al bot "
                           f"({bloqueo}). No se insistirá: se reintenta cada "
                           f"{config.PAUSA_TRAS_BLOQUEO_HORAS} horas y te aviso si vuelve a "
                           "responder. Las demás tiendas siguen normales.")
                s["avisado"] = True
            continue
        if s["fallos"] >= config.FALLOS_PARA_AVISAR and not s.get("avisado") and tg:
            avisar(tg, f"⚠️ <b>{html.escape(nombres[clave])}</b> no responde desde las "
                       f"{hora_lima(s['desde'])} ({s['fallos']} revisiones seguidas).\n"
                       f"Último error: {html.escape(s['error'])}")
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


def texto_estado(est, nombres, ahora):
    ej = list(est.get("ejecuciones") or [])
    if ej:
        ultima = ej[-1][0]
        lineas = [f"✅ <b>Bot activo</b>. Última revisión: {hora_lima(ultima)} "
                  f"(hace {int((ahora - ultima) // 60)} min)"]
    else:
        lineas = ["ℹ️ El bot aún no ha completado ninguna revisión."]
    lineas.append(f"Últimas 24 h: {len(ej)} revisiones · "
                  f"{sum(e[1] for e in ej):,} productos analizados · {sum(e[2] for e in ej)} alertas")
    lineas.append("\n<b>Tiendas</b>")
    salud = est.get("salud") or {}
    for clave, nombre in nombres.items():
        s, nombre = salud.get(clave, {}), html.escape(nombre)
        if s.get("pausa_hasta", 0) > ahora:
            lineas.append(f"⏸️ {nombre}: bloquea al bot ({s.get('bloqueo', '')}); próximo intento "
                          f"a las {hora_lima(s['pausa_hasta'])}")
        elif s.get("fallos"):
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
                  f"{soles(config.MIN_PRECIO_REFERENCIA)} (sin mínimo desde "
                  f"{config.DESCUENTO_SIN_MINIMO:.0%}) · nivel mínimo {config.NIVEL_MINIMO}")
    if est.get("vigilar"):
        lineas.append(f"👁 Vigilando {len(est['vigilar'])} producto(s): /vigilados")
    if silenciado(est, ahora):
        lineas.append(f"🔕 Sin sonido hasta las {hora_lima(est['silencio_hasta'])}")
    return "\n".join(lineas)


def resumen_diario(tg, est, nombres, ahora):
    if config.HORA_RESUMEN_DIARIO < 0:
        return
    local = datetime.fromtimestamp(ahora, LIMA)
    hoy = local.strftime("%Y-%m-%d")
    if local.hour >= config.HORA_RESUMEN_DIARIO and est.get("resumen_diario") != hoy:
        tg.enviar_texto("☀️ <b>Resumen diario</b>\n" + texto_estado(est, nombres, ahora))
        est["resumen_diario"] = hoy
