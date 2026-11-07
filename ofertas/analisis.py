"""Decide qué productos son oportunidades reales y cuáles merecen alerta.

Historial por producto: lista de tramos [inicio, precio]; se
añade un tramo solo cuando el precio cambia. Con eso se calcula el "precio
habitual" (mediana ponderada por tiempo) para confirmar bajadas reales y
descartar descuentos engañosos (precio anterior inflado o descuento eterno)."""

from dataclasses import dataclass, field

import config
from ofertas.modelos import Producto

ERROR, EXTREMA, GRANDE = 3, 2, 1
TITULOS = {
    ERROR: "🚨 ¿ERROR DE PRECIO?",
    EXTREMA: "🔥 OFERTA EXTREMA",
    GRANDE: "⚡ GRAN OFERTA",
}

DIA = 86400
VENTANA_HISTORIAL = 30 * DIA
MIN_OBSERVACION = 3600  # al menos 1 h de historial previo para usarlo
DIAS_AVISO = 7          # un aviso deja de silenciar al producto tras una semana
MAX_TRAMOS = 30         # cambios de precio que se recuerdan por producto


@dataclass
class Evaluacion:
    nivel: int
    descuento: float          # frente al precio anterior de la tienda (0-1)
    caida: float              # frente al precio habitual del historial (0-1)
    habitual: float | None
    referencia: float         # precio con el que se calcula el ahorro
    motivos: list[str] = field(default_factory=list)
    minimo: float | None = None   # precio más bajo visto en los 30 días previos
    veredicto: str = ""           # una línea: ¿es de verdad el mejor precio?
    vigilado: str = ""            # búsqueda de /vigilar que la originó (si la hay)

    @property
    def ahorro_pct(self) -> float:
        return max(self.descuento, self.caida)

    @property
    def titulo(self) -> str:
        return TITULOS[self.nivel]


def _mismo_precio(a: float, b: float) -> bool:
    return abs(a - b) <= 0.005 * max(a, b, 1)


def analizar_historial(h: list | None, precio: float, ahora: float):
    """Devuelve (precio_habitual, dias_con_el_mismo_precio).

    El precio habitual se calcula con los tramos ANTERIORES al precio actual,
    para que una bajada no "contamine" su propia referencia."""
    if not h:
        return None, 0.0
    if _mismo_precio(h[-1][1], precio):
        tramos, fin_ultimo, dias = h[:-1], h[-1][0], (ahora - h[-1][0]) / DIA
    else:
        tramos, fin_ultimo, dias = h, ahora, 0.0
    desde = ahora - VENTANA_HISTORIAL
    pesos = []
    for i, (inicio, p, *_) in enumerate(tramos):
        fin = tramos[i + 1][0] if i + 1 < len(tramos) else fin_ultimo
        duracion = fin - max(inicio, desde)
        if duracion > 0:
            pesos.append((p, duracion))
    total = sum(d for _, d in pesos)
    if total < MIN_OBSERVACION:
        return None, dias
    acumulado = 0
    for p, d in sorted(pesos):
        acumulado += d
        if acumulado >= total / 2:
            return p, dias
    return pesos[-1][0], dias


def minimo_previo(h: list | None, precio: float, ahora: float) -> float | None:
    """Precio más bajo visto en los 30 días previos, sin contar el precio actual
    (detecta la trampa de subir el precio y luego "rebajarlo" al de antes)."""
    if not h:
        return None
    tramos = h[:-1] if _mismo_precio(h[-1][1], precio) else h
    desde = ahora - VENTANA_HISTORIAL
    precios = [p for i, (inicio, p, *_) in enumerate(tramos)
               if (tramos[i + 1][0] if i + 1 < len(tramos) else ahora) >= desde]
    return min(precios) if precios else None


def registrar(est: dict, p: Producto, ahora: float):
    aviso = est.get("a")
    if aviso and (p.precio > aviso["p"] * 1.15 or ahora - aviso["t"] > DIAS_AVISO * DIA):
        # El precio volvió a subir (o pasó una semana): si la oferta regresa,
        # vuelve a ser noticia.
        del est["a"]
    h = est.setdefault("h", [])
    if not h or not _mismo_precio(h[-1][1], p.precio):
        h.append([int(ahora), p.precio])
        del h[:-MAX_TRAMOS]
    est["v"] = int(ahora)


def evaluar(p: Producto, h: list | None, ahora: float) -> Evaluacion | None:
    if p.disponible is False or p.precio <= 0:
        return None

    habitual, dias_mismo_precio = analizar_historial(h, p.precio, ahora)
    minimo = minimo_previo(h, p.precio, ahora)
    descuento = 1 - p.precio / p.precio_antes if p.precio_antes else 0.0
    caida = 1 - p.precio / habitual if habitual and habitual > p.precio else 0.0
    desc_tarjeta = (1 - p.precio_tarjeta / p.precio_antes
                    if p.precio_tarjeta and p.precio_antes else 0.0)
    referencia = max(p.precio_antes or 0, habitual or 0)
    bajo_piso = bool(p.piso and p.precio < p.piso)

    # Con descuentos enormes no importa el precio: se avisa aunque sea barato.
    enorme = max(descuento, caida) >= config.DESCUENTO_SIN_MINIMO
    if not bajo_piso and not enorme:
        if referencia < config.MIN_PRECIO_REFERENCIA:
            return None
        if referencia - p.precio < config.MIN_AHORRO:
            return None

    # Descuento engañoso: el bot ya conoce este producto y su precio actual
    # es prácticamente el de siempre, aunque la tienda muestre un "antes" alto.
    if habitual is not None and caida < 0.15 and not bajo_piso:
        return None
    # Precio inflado y "rebajado": hace poco estuvo más barato que ahora.
    inflado = bool(minimo and p.precio > minimo * 1.05)
    # Descuento permanente: lleva días con el mismo precio, ya no es novedad.
    if dias_mismo_precio >= config.DIAS_DESCUENTO_PERMANENTE and not bajo_piso:
        return None

    motivos = []
    nivel = 0
    # Coma o dígito desplazado: 1,999 -> 199.90 (los dígitos coinciden).
    proporcion = referencia / p.precio if referencia else 0
    tipeo = any(abs(referencia - p.precio * k) <= 0.01 * referencia for k in (10, 100, 1000))

    # Señales de posible error de precio
    if descuento >= config.DESCUENTO_ERROR:
        nivel = ERROR
    if tipeo:
        nivel = ERROR
        motivos.append(f"🔢 Posible coma o dígito de menos (~{round(proporcion)}× más barato)")
    if caida >= 0.75:
        nivel = ERROR
        motivos.append(f"📉 {caida:.0%} bajo su precio habitual")
    if bajo_piso:
        nivel = ERROR
        motivos.append(f"🎯 Precio absurdo para su tipo (piso S/ {p.piso:,.0f})")

    corroborado = tipeo or caida >= config.MIN_CAIDA_HISTORIAL or bajo_piso
    if p.marketplace and nivel == ERROR and not corroborado:
        # Un vendedor externo con 90 % suele ser "precio anterior" inflado.
        nivel = EXTREMA

    if nivel < EXTREMA and (descuento >= 0.80 or caida >= 0.55 or desc_tarjeta >= 0.85):
        nivel = EXTREMA
    if nivel < GRANDE and (descuento >= config.MIN_DESCUENTO or caida >= config.MIN_CAIDA_HISTORIAL):
        nivel = GRANDE
    if inflado and nivel < ERROR:
        # Trampa típica de CyberWow: no es más barato que hace unos días.
        return None
    if nivel == 0 or nivel < config.NIVEL_MINIMO:
        return None
    if p.marketplace and not corroborado and descuento < config.MIN_DESCUENTO_MARKETPLACE:
        return None

    # Veredicto en una línea, según el historial del bot.
    if inflado:
        veredicto = f"⚠️ Hace poco estuvo a S/ {minimo:,.2f}"
    elif minimo and p.precio <= minimo * 1.01:
        veredicto = "✅ Precio más bajo de los últimos 30 días"
    elif habitual and caida >= 0.15:
        veredicto = f"✅ Bajada real: solía costar S/ {habitual:,.2f}"
    else:
        veredicto = "🆕 Sin historial previo para comparar"
    if p.marketplace:
        motivos.append("🏪 Vendedor externo")
    if p.stock is not None and 0 < p.stock <= config.POCAS_UNIDADES:
        motivos.append(f"⏳ Quedan pocas unidades: {p.stock}")
    for etiqueta in p.etiquetas:
        motivos.append(f"🏷️ {etiqueta}")

    return Evaluacion(nivel=nivel, descuento=descuento, caida=caida, habitual=habitual,
                      referencia=referencia, motivos=motivos, minimo=minimo,
                      veredicto=veredicto)


def es_novedad(p: Producto, ev: Evaluacion, est: dict | None, linea_base: bool) -> bool:
    """¿Merece alerta ahora? Evita duplicados y ofertas ya conocidas."""
    est = est or {}
    aviso = est.get("a")
    if aviso:
        # Ya se avisó: solo si bajó todavía más.
        return p.precio <= aviso["p"] * (1 - config.BAJADA_PARA_REAVISAR)
    if est.get("pend"):
        return True  # la alerta anterior no se pudo enviar
    h = est.get("h")
    if h and p.precio >= h[-1][1] * 0.98:
        # El bot ya lo había visto a este precio y no era alerta: no es nuevo.
        return False
    if linea_base and ev.habitual is None and ev.nivel < ERROR:
        # Primera vez que se revisa esta página y el bot no conoce el producto:
        # se registra en silencio para no inundar el chat con ofertas antiguas.
        return False
    return True
