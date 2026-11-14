"""
main.py
=======
Punto de entrada del bot.

Uso:
    python main.py --mode scan     # escanea y registra (sin ejecutar órdenes)
    python main.py --mode paper    # paper trading completo (por defecto)
    python main.py --mode live     # live trading (requiere PHASE=3 + LIVE_TRADING=true)

Inicializa la DB, arranca el bot de comandos de Telegram, lanza el scanner y
gestiona el apagado limpio ante SIGINT/SIGTERM.
"""

from __future__ import annotations

import argparse
import signal
import sys

from config.settings import settings, logger
from db.models import init_db, asegurar_portafolio_inicial, purgar_antiguos
from core.scanner import Scanner
from alerts import telegram as tg


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="polymarket-bot")
    parser.add_argument(
        "--mode",
        choices=["scan", "paper", "live"],
        default="paper",
        help="scan: solo detecta; paper: simula; live: ejecuta órdenes reales.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Recorre el flujo LIVE real (preflight on-chain + revalidación de "
            "orderbook) pero se DETIENE justo antes de enviar la orden. No "
            "arriesga capital ni exige LIVE_TRADING=true. Ideal para validar."
        ),
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help=(
            "Doctor/preflight: verifica clave, RPC, USDC.e (vs nativo), POL, "
            "aprueba allowances y comprueba la auth del CLOB. Da veredicto "
            "LISTO/NO-LISTO. No opera ni arranca el scanner."
        ),
    )
    return parser.parse_args()


def ejecutar_setup() -> None:
    """Verifica de punta a punta que el bot puede operar y aprueba allowances."""
    logger.info("=" * 56)
    logger.info("  SETUP / DOCTOR — verificación previa a operar")
    logger.info("=" * 56)
    problemas: list[str] = []

    # 1. Clave privada.
    if not settings.polymarket_private_key:
        logger.error("❌ POLYMARKET_PRIVATE_KEY no configurada. Genera una wallet "
                     "(eth_account) y ponla en .env. Ver CLAUDE.md.")
        return

    # 2. Wallet + RPC.
    try:
        from feeds.wallet import Wallet, CTF_EXCHANGE, NEG_RISK_EXCHANGE
        w = Wallet()
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ No se pudo inicializar la wallet (¿web3 instalado?): %s", exc)
        return
    logger.info("Dirección: %s", w.address)
    if not w.conectado():
        logger.error("❌ RPC no conecta (%s). Cambia POLYGON_RPC_URL.", w.rpc_url)
        return
    logger.info("✅ RPC conectado: %s", w.rpc_url)

    # 3. Balances.
    usdc = w.balance_usdc()
    matic = w.balance_matic()
    logger.info("Saldo USDC.e: %.4f | POL: %.4f", usdc, matic)
    if usdc <= 0:
        problemas.append("USDC.e en 0. Probablemente tienes USDC NATIVO: haz swap "
                         "a USDC.e (0x2791Bca1...) en QuickSwap.")
    elif usdc < settings.min_operational_capital:
        problemas.append(f"USDC.e ({usdc:.2f}) < capital mínimo operacional "
                         f"({settings.min_operational_capital}).")
    if matic <= 0:
        problemas.append("POL en 0: sin gas no puedes aprobar allowances ni operar. "
                         "Retira ~3 USD de POL.")

    # 4. Allowances (solo si hay gas).
    if matic > 0:
        for nombre, ex in (("CTF", CTF_EXCHANGE), ("NegRisk", NEG_RISK_EXCHANGE)):
            try:
                h = w.asegurar_allowance(ex)
                logger.info("Allowance %s: %s", nombre, "ya OK" if h is None else h)
            except Exception as exc:  # noqa: BLE001
                problemas.append(f"No se pudo aprobar allowance {nombre}: {exc}")
    else:
        problemas.append("Sin POL no se aprobaron allowances.")

    # 5. Auth del CLOB (lo único que el dry-run no toca).
    try:
        from feeds.polymarket import PolymarketClient
        prev_live, prev_phase = settings.live_trading, settings.phase
        settings.live_trading, settings.phase = True, 3  # forzar init del CLOB
        addr = PolymarketClient()._get_clob().get_address()
        settings.live_trading, settings.phase = prev_live, prev_phase
        logger.info("✅ CLOB auth OK: %s", addr)
    except Exception as exc:  # noqa: BLE001
        problemas.append(f"Auth del CLOB falló: {exc}")

    # Veredicto.
    logger.info("=" * 56)
    if problemas:
        logger.warning("⚠️  NO LISTO. Pendientes:")
        for p in problemas:
            logger.warning("   - %s", p)
    else:
        logger.info("✅✅ TODO LISTO. Siguiente: python main.py --dry-run")
    logger.info("=" * 56)


def validar_modo(mode: str, dry_run: bool = False) -> bool:
    """Devuelve True si se deben ejecutar operaciones (paper, live o dry-run)."""
    if dry_run:
        logger.warning(
            "🟡 DRY-RUN: se recorrerá el flujo LIVE real (preflight + orderbook) "
            "pero NO se enviará ninguna orden."
        )
        return True
    if mode == "live":
        if not settings.es_live:
            logger.error(
                "Modo 'live' solicitado pero LIVE_TRADING/PHASE no lo permiten. "
                "Configura PHASE=3 y LIVE_TRADING=true. Abortando por seguridad."
            )
            sys.exit(2)
        logger.warning("⚠️  MODO LIVE: se ejecutarán órdenes REALES.")
        return True
    if mode == "paper":
        if settings.es_live:
            logger.warning(
                "LIVE_TRADING está activo pero se solicitó modo paper: no se "
                "enviarán órdenes reales."
            )
        return True
    # mode == scan
    logger.info("Modo SCAN: solo detección y registro, sin ejecutar operaciones.")
    return False


def main() -> None:
    args = parse_args()

    # Setup/doctor: verifica y aprueba allowances, luego sale (no arranca nada).
    if args.setup:
        ejecutar_setup()
        return

    # Activa dry-run lo antes posible para que se refleje en el resumen y en
    # toda la lógica del executor.
    if args.dry_run:
        settings.dry_run = True

    logger.info("Arrancando polymarket-bot")
    logger.info("\n%s", settings.resumen())

    # Validación de configuración antes de tocar nada externo.
    try:
        settings.validar()
    except ValueError as exc:
        logger.error(str(exc))
        sys.exit(1)

    ejecutar = validar_modo(args.mode, dry_run=args.dry_run)

    # DB.
    init_db()
    asegurar_portafolio_inicial()

    # Poda inicial: en una corrida 24/7 la tabla `mercados` crece sin límite
    # (un snapshot por mercado y ciclo). Limpiamos lo viejo al arrancar; el
    # scanner repite la poda cada hora. Defensivo: nunca debe impedir el arranque.
    try:
        borrado = purgar_antiguos(settings.db_retention_days)
        if borrado["mercados"] or borrado["logs"]:
            logger.info(
                "Poda inicial de DB: %s mercados y %s logs > %sd eliminados.",
                borrado["mercados"], borrado["logs"], settings.db_retention_days,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Poda inicial de DB falló: %s", exc)

    # Telegram (comandos en hilo daemon).
    tg.iniciar_bot_comandos()
    modo_str = "dry-run" if args.dry_run else args.mode
    tg.notifier.enviar_mensaje(f"🤖 polymarket-bot iniciado en modo *{modo_str}*.")

    scanner = Scanner()

    # Apagado limpio.
    def _shutdown(signum, _frame):
        logger.info("Señal %s recibida, deteniendo...", signum)
        scanner.detener()
        tg.notifier.enviar_mensaje("🛑 polymarket-bot detenido.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Loop principal. En modo scan, ejecutar=False (no opera).
    if ejecutar:
        scanner.loop()
    else:
        # Loop de solo-detección reutilizando el mismo motor.
        import time

        while True:
            scanner.escanear_una_vez(ejecutar=False)
            time.sleep(settings.scan_interval_seconds)


if __name__ == "__main__":
    main()
