"""Mensajes de registro (consola, registros de GitHub o datos/local.log).

En un repositorio público los registros de GitHub los puede ver cualquiera:
en modo discreto solo se muestran totales (sin tiendas, categorías ni errores
detallados). Los detalles se consultan en Telegram con /estado."""

import threading

from ofertas.telegram import hora_lima

_candado = threading.Lock()
discreto = False


def log(msg: str, detalle: bool = False):
    """detalle=True: mensaje que revela la receta; se omite en modo discreto."""
    if detalle and discreto:
        return
    with _candado:  # varios hilos escriben a la vez
        print(f"[{hora_lima()}] {msg}", flush=True)
