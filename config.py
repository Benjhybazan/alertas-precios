"""
Configuración del bot.

La configuración real (tiendas, direcciones, filtros, categorías, pisos de
precio y umbrales) NO está en este archivo ni en el repositorio. Se lee de:
  * en GitHub: el Secret CONFIGURACION;
  * en tu PC: el archivo config_privada.json (excluido de git).
Aquí solo quedan valores genéricos de respaldo.

Prioridad de cada umbral: variable de entorno > configuración privada > respaldo.
"""

import json
import os

RUTA_PRIVADA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_privada.json")


def _cargar_privada():
    texto = os.environ.get("CONFIGURACION", "").strip()
    origen = "Secret CONFIGURACION"
    if not texto and os.path.exists(RUTA_PRIVADA):
        with open(RUTA_PRIVADA, encoding="utf-8") as f:
            texto, origen = f.read(), "config_privada.json"
    if not texto:
        return {}, "ninguna"
    try:
        datos = json.loads(texto)
    except ValueError as e:
        # No se muestra el contenido: podría revelar la configuración.
        raise SystemExit(f"La configuración privada ({origen}) no es un JSON válido: "
                         f"línea {getattr(e, 'lineno', '?')}")
    return (datos if isinstance(datos, dict) else {}), origen


PRIVADA, ORIGEN_CONFIGURACION = _cargar_privada()
_UMBRALES = PRIVADA.get("umbrales") or {}


def _num(nombre, defecto):
    valor = os.environ.get(nombre, "").strip()
    if not valor:
        valor = _UMBRALES.get(nombre)
    if valor is None or valor == "":
        return defecto
    try:
        return type(defecto)(float(valor))
    except (TypeError, ValueError):
        return defecto


# ---------------------------------------------------------------------------
# Umbrales de detección (respaldo; los reales están en la configuración privada)
# ---------------------------------------------------------------------------

# Descuento mínimo (0.70 = 70 %) frente al precio anterior que muestra la tienda.
MIN_DESCUENTO = _num("MIN_DESCUENTO", 0.70)
# Vendedores externos (marketplace): se les exige más descuento, salvo que el
# historial confirme una bajada real de precio.
MIN_DESCUENTO_MARKETPLACE = _num("MIN_DESCUENTO_MARKETPLACE", 0.80)
# Descuento a partir del cual se considera "posible error de precio".
DESCUENTO_ERROR = _num("DESCUENTO_ERROR", 0.90)
# Bajada mínima frente al precio habitual observado por el bot (historial).
MIN_CAIDA_HISTORIAL = _num("MIN_CAIDA_HISTORIAL", 0.45)
# Precio de referencia mínimo y ahorro mínimo (soles).
MIN_PRECIO_REFERENCIA = _num("MIN_PRECIO_REFERENCIA", 120.0)
MIN_AHORRO = _num("MIN_AHORRO", 80.0)
# Nivel mínimo que se notifica: 1 = todo, 2 = extremas y errores, 3 = solo errores.
NIVEL_MINIMO = _num("NIVEL_MINIMO", 1)
# Unidades a partir de las cuales se avisa "quedan pocas unidades".
POCAS_UNIDADES = _num("POCAS_UNIDADES", 5)
# Días con el mismo precio tras los que un descuento se considera permanente.
DIAS_DESCUENTO_PERMANENTE = _num("DIAS_DESCUENTO_PERMANENTE", 3)
# Bajada adicional necesaria para volver a avisar del mismo producto.
BAJADA_PARA_REAVISAR = _num("BAJADA_PARA_REAVISAR", 0.05)
# Alertas con foto por ejecución; el resto va en un mensaje resumen.
MAX_ALERTAS_POR_EJECUCION = _num("MAX_ALERTAS_POR_EJECUCION", 10)
# Resumen diario "el bot sigue vivo" (hora de Lima, 0-23). -1 lo desactiva.
HORA_RESUMEN_DIARIO = _num("HORA_RESUMEN_DIARIO", 9)

# Avisar si una tienda falla en esta cantidad de ejecuciones seguidas.
FALLOS_PARA_AVISAR = 3
# Productos no vistos en estos días se borran del historial.
DIAS_RETENCION = 21

# ---------------------------------------------------------------------------
# Repositorio privado en GitHub
# ---------------------------------------------------------------------------
# GitHub regala 2.000 minutos al mes en repositorios privados y cobra cada
# ejecución redondeando al minuto. Estos topes de seguridad evitan que un día
# lento gaste de más: lo que falte se lee al principio de la siguiente ejecución.
MINUTOS_GRATIS_MES = 2000
SEGUNDOS_DESCARGA_PRIVADO = 40
SEGUNDOS_TOTAL_PRIVADO = 45

# ---------------------------------------------------------------------------
# Descarga
# ---------------------------------------------------------------------------

# Identificación honesta del bot frente a las tiendas.
USER_AGENT = "Mozilla/5.0 (compatible; OfertasPeruBot/1.0; uso personal)"
# Pausa (segundos) entre peticiones de una misma conexión, para no sobrecargar.
PAUSA_ENTRE_PETICIONES = 1.0
TIMEOUT_SEGUNDOS = 30

# ---------------------------------------------------------------------------
# Tiendas: solo en la configuración privada
# ---------------------------------------------------------------------------
TIENDAS = PRIVADA.get("tiendas") or {}
