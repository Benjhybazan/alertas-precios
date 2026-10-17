"""Descargas HTTP con pausas entre peticiones y sin reintentos agresivos."""

import shutil
import subprocess
import time

import httpx

import config


class ErrorDescarga(Exception):
    def __init__(self, mensaje, codigo=None):
        super().__init__(mensaje)
        self.codigo = codigo


class Descargador:
    """Un descargador por conexión: respeta una pausa mínima entre peticiones.

    usar_curl=True usa el programa curl del sistema en vez de httpx (se activa
    por tienda en la configuración privada), con la misma identificación
    honesta del bot."""

    def __init__(self, usar_curl=False, limite=None):
        self.usar_curl = usar_curl
        self.limite = limite  # time.monotonic() a partir del cual no empezar más páginas
        self._ultima = 0.0
        self._cliente = None
        if usar_curl:
            self._curl = shutil.which("curl")
            if not self._curl:
                raise ErrorDescarga("curl no está instalado")
        else:
            self._cliente = httpx.Client(
                headers={
                    "User-Agent": config.USER_AGENT,
                    "Accept-Language": "es-PE,es;q=0.9",
                },
                timeout=config.TIMEOUT_SEGUNDOS,
                follow_redirects=True,
            )

    def sin_tiempo(self) -> bool:
        return bool(self.limite) and time.monotonic() > self.limite

    def cerrar(self):
        if self._cliente:
            self._cliente.close()

    def _esperar_turno(self):
        espera = config.PAUSA_ENTRE_PETICIONES - (time.monotonic() - self._ultima)
        if espera > 0:
            time.sleep(espera)
        self._ultima = time.monotonic()

    def texto(self, url: str) -> str:
        # Un solo reintento, y solo ante fallos de red o errores 5xx.
        for intento in (1, 2):
            self._esperar_turno()
            try:
                codigo, cuerpo = self._curl_get(url) if self.usar_curl else self._httpx_get(url)
            except (httpx.HTTPError, subprocess.TimeoutExpired, OSError) as e:
                if intento == 2:
                    raise ErrorDescarga(f"error de red: {e.__class__.__name__}") from e
                time.sleep(3)
                continue
            if codigo >= 500 and intento == 1:
                time.sleep(3)
                continue
            if codigo >= 400:
                raise ErrorDescarga(f"HTTP {codigo}", codigo)
            return cuerpo
        raise ErrorDescarga("sin respuesta")

    def _httpx_get(self, url):
        r = self._cliente.get(url)
        return r.status_code, r.text

    def _curl_get(self, url):
        marca = "\n__HTTP_CODE__:"
        proc = subprocess.run(
            [self._curl, "-sS", "-L", "--compressed",
             "-m", str(config.TIMEOUT_SEGUNDOS),
             "-A", config.USER_AGENT,
             "-H", "Accept-Language: es-PE,es;q=0.9",
             "-w", marca + "%{http_code}", url],
            capture_output=True, timeout=config.TIMEOUT_SEGUNDOS + 10,
        )
        salida = proc.stdout.decode("utf-8", errors="replace")
        if proc.returncode != 0 and marca not in salida:
            raise ErrorDescarga(f"curl falló (código {proc.returncode}): "
                                f"{proc.stderr.decode(errors='replace').strip()[:120]}")
        cuerpo, _, codigo = salida.rpartition(marca)
        return int(codigo or 0), cuerpo
