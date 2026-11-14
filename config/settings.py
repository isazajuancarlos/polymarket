"""
config/settings.py
==================
Carga centralizada de configuración desde variables de entorno (.env),
validación de parámetros al inicio, definición de fases de operación y
configuración de logging.

Importa `settings` desde aquí en cualquier módulo:

    from config.settings import settings, logger
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Dict

from dotenv import load_dotenv

# Carga el .env del directorio raíz del proyecto (si existe).
load_dotenv()


# ---------------------------------------------------------------------------
# Helpers de parseo de entorno
# ---------------------------------------------------------------------------
def _get_str(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _get_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"La variable de entorno {key}={raw!r} no es un número válido") from exc


def _get_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"La variable de entorno {key}={raw!r} no es un entero válido") from exc


# ---------------------------------------------------------------------------
# Fases de operación
# ---------------------------------------------------------------------------
# Fase 1: paper trading puro, sin órdenes reales. Detecta y registra.
# Fase 2: paper trading + alertas en vivo + (opcional) capital reducido.
# Fase 3: live trading completo con todos los controles de riesgo activos.
PHASES: Dict[int, Dict[str, object]] = {
    1: {
        "nombre": "Paper Trading (validación)",
        "allow_live": False,
        "capital_factor": 1.0,
        "descripcion": "Solo simulación. Registra cada decisión para validación manual.",
    },
    2: {
        "nombre": "Paper + Alertas en vivo",
        "allow_live": False,
        "capital_factor": 0.5,
        "descripcion": "Simulación con datos en tiempo real y alertas Telegram activas.",
    },
    3: {
        "nombre": "Live Trading",
        "allow_live": True,
        "capital_factor": 1.0,
        "descripcion": "Ejecución real de órdenes con controles de riesgo completos.",
    },
}


# ---------------------------------------------------------------------------
# Objeto de configuración
# ---------------------------------------------------------------------------
@dataclass
class Settings:
    # Credenciales
    polymarket_api_key: str = field(default_factory=lambda: _get_str("POLYMARKET_API_KEY"))
    polymarket_private_key: str = field(default_factory=lambda: _get_str("POLYMARKET_PRIVATE_KEY"))
    anthropic_api_key: str = field(default_factory=lambda: _get_str("ANTHROPIC_API_KEY"))
    telegram_bot_token: str = field(default_factory=lambda: _get_str("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _get_str("TELEGRAM_CHAT_ID"))
    database_url: str = field(
        default_factory=lambda: _get_str("DATABASE_URL", "sqlite:///polymarket_bot.db")
    )

    # Modo
    live_trading: bool = field(default_factory=lambda: _get_bool("LIVE_TRADING", False))
    phase: int = field(default_factory=lambda: _get_int("PHASE", 1))
    # Dry-run: recorre el flujo LIVE real (preflight + revalidación de orderbook)
    # pero NO envía órdenes. Lo activa main.py con --dry-run; nunca debe quedar
    # fijo en .env para operar de verdad.
    dry_run: bool = field(default_factory=lambda: _get_bool("DRY_RUN", False))

    # Capital y riesgo
    capital_usdc: float = field(default_factory=lambda: _get_float("CAPITAL_USDC", 500.0))
    max_position_pct: float = field(default_factory=lambda: _get_float("MAX_POSITION_PCT", 0.10))
    stop_loss_pct: float = field(default_factory=lambda: _get_float("STOP_LOSS_PCT", 0.08))
    arb_threshold: float = field(default_factory=lambda: _get_float("ARB_THRESHOLD", 0.97))
    kelly_fraction: float = field(default_factory=lambda: _get_float("KELLY_FRACTION", 0.25))
    min_ev: float = field(default_factory=lambda: _get_float("MIN_EV", 0.05))
    min_liquidity: float = field(default_factory=lambda: _get_float("MIN_LIQUIDITY", 200.0))

    # Operativos
    fee: float = field(default_factory=lambda: _get_float("FEE", 0.02))
    min_operational_capital: float = field(
        default_factory=lambda: _get_float("MIN_OPERATIONAL_CAPITAL", 300.0)
    )
    max_concurrent_trades: int = field(
        default_factory=lambda: _get_int("MAX_CONCURRENT_TRADES", 3)
    )
    scan_interval_seconds: int = field(
        default_factory=lambda: _get_int("SCAN_INTERVAL_SECONDS", 30)
    )
    # Detección binaria honesta: cuántos mercados confirmar por ciclo con el
    # orderbook REAL (best_ask de cada lado). No se pueden confirmar los ~500
    # cada 30s (rate limit 5 req/s = 2 llamadas por mercado); se confirma una
    # ventana rotatoria de este tamaño y se barre el universo en varios ciclos.
    deep_check_binary_max: int = field(
        default_factory=lambda: _get_int("DEEP_CHECK_BINARY_MAX", 20)
    )
    # Retención de la DB. El scanner persiste un snapshot de CADA mercado en
    # CADA ciclo (~1-2M filas/día corriendo 24/7): sin poda, el archivo SQLite
    # crece sin límite y llena el disco del VPS. Se borran los snapshots de
    # `mercados` y los `logs` más antiguos que esto. 0 = desactiva la poda.
    db_retention_days: int = field(
        default_factory=lambda: _get_int("DB_RETENTION_DAYS", 7)
    )

    # Market making (estrategia 3)
    mm_min_volume: float = field(default_factory=lambda: _get_float("MM_MIN_VOLUME", 5000.0))
    mm_half_spread: float = field(default_factory=lambda: _get_float("MM_HALF_SPREAD", 0.02))
    mm_min_edge: float = field(default_factory=lambda: _get_float("MM_MIN_EDGE", 0.005))
    mm_skew: float = field(default_factory=lambda: _get_float("MM_SKEW", 0.5))
    mm_max_markets: int = field(default_factory=lambda: _get_int("MM_MAX_MARKETS", 3))
    # Banda de precio para market making: fuera de [min, max] el outcome es
    # demasiado extremo (longshot o casi-seguro) y proveer liquidez es una
    # trampa de inventario. Evita cotizar en mercados basura.
    mm_min_price: float = field(default_factory=lambda: _get_float("MM_MIN_PRICE", 0.15))
    mm_max_price: float = field(default_factory=lambda: _get_float("MM_MAX_PRICE", 0.85))

    # EV / modelo de probabilidad
    # EV_ENABLED activa la DETECCIÓN de EV (requiere un modelo real, p. ej. LLM).
    # EV_LIVE permite ÓRDENES REALES de EV; separado de LIVE_TRADING a propósito,
    # para que reactivar EV en vivo sea un acto deliberado tras validar en paper.
    ev_enabled: bool = field(default_factory=lambda: _get_bool("EV_ENABLED", False))
    ev_live: bool = field(default_factory=lambda: _get_bool("EV_LIVE", False))
    # El modelo debe discrepar del precio al menos esto (absoluto) para operar.
    ev_min_divergence: float = field(
        default_factory=lambda: _get_float("EV_MIN_DIVERGENCE", 0.10)
    )
    llm_model: str = field(
        default_factory=lambda: _get_str("LLM_MODEL", "claude-haiku-4-5-20251001")
    )

    # Arbitraje multi-resultado (estructural, "lay the field")
    multi_arb_enabled: bool = field(
        default_factory=lambda: _get_bool("MULTI_ARB_ENABLED", False)
    )
    multi_arb_live: bool = field(default_factory=lambda: _get_bool("MULTI_ARB_LIVE", False))
    multi_arb_max_outcomes: int = field(
        default_factory=lambda: _get_int("MULTI_ARB_MAX_OUTCOMES", 8)
    )
    multi_arb_min_profit: float = field(
        default_factory=lambda: _get_float("MULTI_ARB_MIN_PROFIT", 0.01)
    )
    # Techo de seguridad: un arbitraje estructural REAL es pequeño. Un profit por
    # encima de esto casi siempre significa que la suposición falla (evento no
    # excluyente, libro incompleto, precios stale) -> se omite y se avisa.
    multi_arb_max_profit: float = field(
        default_factory=lambda: _get_float("MULTI_ARB_MAX_PROFIT", 0.15)
    )

    # Wallet / on-chain
    polygon_rpc_url: str = field(
        default_factory=lambda: _get_str("POLYGON_RPC_URL", "https://polygon-rpc.com")
    )

    # Ejecución LIVE (taker)
    # Slippage máximo tolerado al cruzar el libro con órdenes FOK. Se usa para
    # fijar el límite de precio y para verificar que el arbitraje SIGUE siendo
    # rentable a precios ejecutables antes de enviar nada.
    live_slippage: float = field(default_factory=lambda: _get_float("LIVE_SLIPPAGE", 0.005))

    def validar(self) -> None:
        """Valida coherencia de parámetros. Lanza ValueError si algo es inseguro."""
        errores = []

        if not (0 < self.max_position_pct <= 1):
            errores.append("MAX_POSITION_PCT debe estar en (0, 1].")
        if not (0 < self.stop_loss_pct < 1):
            errores.append("STOP_LOSS_PCT debe estar en (0, 1).")
        if not (0 < self.arb_threshold <= 1):
            errores.append("ARB_THRESHOLD debe estar en (0, 1].")
        if not (0 < self.kelly_fraction <= 1):
            errores.append("KELLY_FRACTION debe estar en (0, 1].")
        if self.min_ev < 0:
            errores.append("MIN_EV no puede ser negativo.")
        if self.min_liquidity < 0:
            errores.append("MIN_LIQUIDITY no puede ser negativo.")
        if not (0 <= self.fee < 1):
            errores.append("FEE debe estar en [0, 1).")
        if self.capital_usdc <= 0:
            errores.append("CAPITAL_USDC debe ser positivo.")
        if self.min_operational_capital > self.capital_usdc:
            errores.append("MIN_OPERATIONAL_CAPITAL no puede superar CAPITAL_USDC.")
        if self.max_concurrent_trades < 1:
            errores.append("MAX_CONCURRENT_TRADES debe ser >= 1.")
        if self.db_retention_days < 0:
            errores.append("DB_RETENTION_DAYS no puede ser negativo (0 = sin poda).")
        if self.scan_interval_seconds < 1:
            errores.append("SCAN_INTERVAL_SECONDS debe ser >= 1.")
        if self.deep_check_binary_max < 1:
            errores.append("DEEP_CHECK_BINARY_MAX debe ser >= 1.")
        if not (0 < self.mm_half_spread < 0.5):
            errores.append("MM_HALF_SPREAD debe estar en (0, 0.5).")
        if self.mm_min_edge < 0:
            errores.append("MM_MIN_EDGE no puede ser negativo.")
        if not (0 <= self.mm_skew <= 2):
            errores.append("MM_SKEW debe estar en [0, 2].")
        if self.mm_max_markets < 1:
            errores.append("MM_MAX_MARKETS debe ser >= 1.")
        if not (0 <= self.live_slippage < 0.1):
            errores.append("LIVE_SLIPPAGE debe estar en [0, 0.1).")
        if not (0 < self.mm_min_price < self.mm_max_price < 1):
            errores.append("Se requiere 0 < MM_MIN_PRICE < MM_MAX_PRICE < 1.")
        if not (0 <= self.ev_min_divergence < 1):
            errores.append("EV_MIN_DIVERGENCE debe estar en [0, 1).")
        if self.ev_live and not self.ev_enabled:
            errores.append("EV_LIVE=true requiere EV_ENABLED=true.")
        if self.multi_arb_live and not self.multi_arb_enabled:
            errores.append("MULTI_ARB_LIVE=true requiere MULTI_ARB_ENABLED=true.")
        if not (2 <= self.multi_arb_max_outcomes <= 20):
            errores.append("MULTI_ARB_MAX_OUTCOMES debe estar en [2, 20].")
        if self.multi_arb_min_profit < 0:
            errores.append("MULTI_ARB_MIN_PROFIT no puede ser negativo.")
        if self.multi_arb_max_profit <= self.multi_arb_min_profit:
            errores.append("MULTI_ARB_MAX_PROFIT debe ser > MULTI_ARB_MIN_PROFIT.")
        if self.phase not in PHASES:
            errores.append(f"PHASE debe ser una de {list(PHASES)}.")

        # Coherencia entre fase y LIVE_TRADING.
        if self.live_trading and not PHASES[self.phase]["allow_live"]:
            errores.append(
                f"LIVE_TRADING=true no está permitido en la fase {self.phase} "
                f"({PHASES[self.phase]['nombre']}). Usa PHASE=3 para operar en vivo."
            )

        if errores:
            raise ValueError("Configuración inválida:\n  - " + "\n  - ".join(errores))

    @property
    def fase_info(self) -> Dict[str, object]:
        return PHASES[self.phase]

    @property
    def es_live(self) -> bool:
        """True solo si LIVE_TRADING está activo Y la fase lo permite."""
        return self.live_trading and bool(PHASES[self.phase]["allow_live"])

    def resumen(self) -> str:
        if self.dry_run:
            modo = "DRY-RUN 🟡"
        elif self.es_live:
            modo = "LIVE 🔴"
        else:
            modo = "PAPER 🟢"
        return (
            f"Modo: {modo} | Fase {self.phase} ({self.fase_info['nombre']})\n"
            f"Capital: {self.capital_usdc} USDC | Max pos: {self.max_position_pct:.0%} | "
            f"Stop loss: {self.stop_loss_pct:.0%}\n"
            f"Arb<{self.arb_threshold} | Kelly×{self.kelly_fraction} | "
            f"EV_min={self.min_ev:.0%} | Liq_min={self.min_liquidity} | Fee={self.fee:.0%}"
        )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def configurar_logging(nivel: int = logging.INFO) -> logging.Logger:
    log = logging.getLogger("polymarket_bot")
    if log.handlers:
        return log  # ya configurado

    log.setLevel(nivel)
    handler = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(fmt)
    log.addHandler(handler)
    log.propagate = False
    return log


# Instancias singleton importables.
settings = Settings()
logger = configurar_logging()
