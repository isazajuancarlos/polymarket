"""
alerts/telegram.py
==================
Bot de Telegram para control y monitoreo:

Comandos:
    /status        -> capital actual, ops hoy, ganancia acumulada
    /pausar        -> pausa el bot inmediatamente
    /reanudar      -> reanuda el bot
    /reporte       -> resumen últimos 7 días con métricas
    /config        -> parámetros actuales
    /oportunidades -> mejores oportunidades detectadas hoy

Alertas automáticas:
    - enviar_alerta_arbitraje()  -> cuando hay arbitraje > 5%
    - enviar_alerta_stop_loss()  -> cuando el stop loss se activa

El módulo funciona en dos planos:
    1. Notificador "fire-and-forget" (enviar_mensaje), usable desde el scanner
       de forma síncrona sin depender del polling de comandos.
    2. Aplicación de comandos (python-telegram-bot) que corre en su propio hilo.

Si no hay TELEGRAM_BOT_TOKEN configurado, todo degrada a logs (no rompe).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import requests

from config.settings import settings, logger
from core.risk import risk_manager
from db.models import get_session, Operacion
from paper_trading.simulator import PaperSimulator


# Cache en memoria de oportunidades del día (lo llena el scanner).
_oportunidades_hoy: List[dict] = []
_lock = threading.Lock()


def registrar_oportunidad(decision: dict) -> None:
    with _lock:
        _oportunidades_hoy.append({**decision, "ts": datetime.now(timezone.utc)})
        # Mantener acotado.
        if len(_oportunidades_hoy) > 200:
            del _oportunidades_hoy[:-200]


def _oportunidades_del_dia() -> List[dict]:
    hoy = datetime.now(timezone.utc).date()
    with _lock:
        return [o for o in _oportunidades_hoy if o["ts"].date() == hoy]


# ---------------------------------------------------------------------------
# Notificador simple (síncrono, vía HTTP)
# ---------------------------------------------------------------------------
class TelegramNotifier:
    def __init__(self) -> None:
        self.token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id
        self.activo = bool(self.token and self.chat_id)
        if not self.activo:
            logger.info("Telegram no configurado: las alertas se enviarán solo al log.")

    def enviar_mensaje(self, texto: str) -> None:
        if not self.activo:
            logger.info("[TELEGRAM:off] %s", texto)
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": texto, "parse_mode": "Markdown"},
                timeout=10,
            )
        except requests.RequestException as exc:
            logger.warning("No se pudo enviar mensaje Telegram: %s", exc)

    # --- Alertas automáticas ---
    def enviar_alerta_arbitraje(self, decision: dict) -> None:
        pct = decision.get("ganancia_garantizada_pct", 0.0)
        self.enviar_mensaje(
            f"🟢 *ARBITRAJE detectado* ({pct:.2%})\n"
            f"{decision.get('titulo', '')[:80]}\n"
            f"Apuesta SÍ: {decision.get('apuesta_si', 0):.2f} | "
            f"NO: {decision.get('apuesta_no', 0):.2f}\n"
            f"Ganancia mínima: {decision.get('ganancia_minima_usd', 0):.2f} USDC"
        )

    def enviar_alerta_ev(self, decision: dict) -> None:
        self.enviar_mensaje(
            f"🔵 *EV positivo* ({decision.get('ev', 0):.2%})\n"
            f"{decision.get('titulo', '')[:80]}\n"
            f"Apuesta sugerida (Kelly): {decision.get('apuesta', 0):.2f} USDC"
        )

    def enviar_alerta_stop_loss(self) -> None:
        rep = risk_manager.reporte_exposicion()
        self.enviar_mensaje(
            "🔴 *STOP LOSS ACTIVADO*\n"
            f"Capital: {rep['capital_actual']:.2f} / {rep['capital_inicial']:.2f}\n"
            f"Drawdown: {rep['drawdown_pct']:.2%}\n"
            "El bot ha sido pausado automáticamente."
        )


notifier = TelegramNotifier()


# ---------------------------------------------------------------------------
# Texto de respuesta de comandos (reutilizable con o sin PTB)
# ---------------------------------------------------------------------------
def _texto_status() -> str:
    rep = risk_manager.reporte_exposicion()
    desde = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    with get_session() as s:
        ops_hoy = s.query(Operacion).filter(Operacion.timestamp >= desde).count()
    estado = "⏸️ PAUSADO" if rep["pausado"] else "▶️ ACTIVO"
    desbalance = rep.get("posiciones_desbalanceadas", 0)
    alerta_desbalance = (
        f"\n⚠️ *Posiciones sin cubrir:* {desbalance}" if desbalance else ""
    )
    return (
        f"*Estado:* {estado}\n"
        f"*Capital actual:* {rep['capital_actual']:.2f} USDC\n"
        f"*Ganancia acumulada:* {rep['ganancia_acumulada']:+.2f} USDC\n"
        f"*Operaciones hoy:* {ops_hoy}\n"
        f"*Operaciones abiertas:* {rep['operaciones_abiertas']}\n"
        f"*Drawdown:* {rep['drawdown_pct']:.2%}"
        f"{alerta_desbalance}"
    )


def _texto_reporte() -> str:
    m = PaperSimulator().calcular_metricas(dias=7)
    return (
        "*Reporte 7 días*\n"
        f"Operaciones: {m.operaciones}\n"
        f"Win rate: {m.win_rate:.1%}\n"
        f"Ganancia acumulada: {m.ganancia_acumulada:+.2f} USDC\n"
        f"ROI: {m.roi:+.2%}\n"
        f"Drawdown máximo: {m.drawdown_maximo:.2%}"
    )


def _texto_config() -> str:
    return "*Configuración actual*\n```\n" + settings.resumen() + "\n```"


def _texto_oportunidades() -> str:
    ops = _oportunidades_del_dia()
    if not ops:
        return "No hay oportunidades registradas hoy."
    ops = sorted(ops, key=lambda d: d.get("ganancia_minima_usd", 0.0), reverse=True)[:10]
    lineas = ["*Mejores oportunidades de hoy*"]
    for i, o in enumerate(ops, 1):
        lineas.append(
            f"{i}. [{o.get('tipo')}] {o.get('titulo', '')[:50]} "
            f"→ {o.get('ganancia_minima_usd', 0):.2f} USDC"
        )
    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Aplicación de comandos (python-telegram-bot), opcional en su propio hilo
# ---------------------------------------------------------------------------
def iniciar_bot_comandos() -> Optional[threading.Thread]:
    """Arranca el polling de comandos en un hilo daemon. Devuelve el hilo o None."""
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        logger.info("Bot de comandos Telegram desactivado (sin token/chat_id).")
        return None

    try:
        from telegram import Update
        from telegram.ext import (
            Application,
            CommandHandler,
            ContextTypes,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("python-telegram-bot no disponible: %s", exc)
        return None

    autorizado = str(settings.telegram_chat_id)

    def _check(update) -> bool:
        return str(update.effective_chat.id) == autorizado

    async def status(update: "Update", _ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        if _check(update):
            await update.message.reply_markdown(_texto_status())

    async def pausar(update: "Update", _ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        if _check(update):
            risk_manager.pausar()
            await update.message.reply_text("⏸️ Bot pausado.")

    async def reanudar(update: "Update", _ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        if _check(update):
            risk_manager.reanudar()
            await update.message.reply_text("▶️ Bot reanudado.")

    async def reporte(update: "Update", _ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        if _check(update):
            await update.message.reply_markdown(_texto_reporte())

    async def config(update: "Update", _ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        if _check(update):
            await update.message.reply_markdown(_texto_config())

    async def oportunidades(update: "Update", _ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        if _check(update):
            await update.message.reply_markdown(_texto_oportunidades())

    def _run() -> None:
        import asyncio

        asyncio.set_event_loop(asyncio.new_event_loop())
        app = Application.builder().token(settings.telegram_bot_token).build()
        app.add_handler(CommandHandler("status", status))
        app.add_handler(CommandHandler("pausar", pausar))
        app.add_handler(CommandHandler("reanudar", reanudar))
        app.add_handler(CommandHandler("reporte", reporte))
        app.add_handler(CommandHandler("config", config))
        app.add_handler(CommandHandler("oportunidades", oportunidades))
        logger.info("Bot de comandos Telegram iniciado.")
        app.run_polling(stop_signals=None)

    hilo = threading.Thread(target=_run, name="telegram-commands", daemon=True)
    hilo.start()
    return hilo
