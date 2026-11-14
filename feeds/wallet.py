"""
feeds/wallet.py
===============
Gestión on-chain del wallet (Polygon) con web3.py.

Responde a la pregunta de diseño: para un bot conviene un **EOA propio y
dedicado** cuya clave privada controlas (POLYMARKET_PRIVATE_KEY), separado de
tus fondos personales y con solo el capital operativo + algo de MATIC para gas.

Reparto de responsabilidades:
    - Firmar/enviar ÓRDENES  -> py-clob-client (EIP-712, gasless, off-chain).
    - Allowance / balances    -> este módulo (web3.py, on-chain).

Funciones:
    - balance_usdc()                  -> USDC disponible (lectura)
    - balance_matic()                 -> MATIC para gas (lectura)
    - allowance(spender)              -> allowance USDC concedida a un contrato
    - asegurar_allowance(spender)     -> aprueba (una vez) si falta allowance

web3 se importa de forma perezosa: el resto del bot (paper trading, tests) no
necesita tenerlo instalado.

⚠️ Verifica las direcciones de contrato antes de operar en LIVE: pueden cambiar
y dependen de si el mercado usa el CTF Exchange estándar o el Neg-Risk Exchange.
"""

from __future__ import annotations

from typing import Optional

from config.settings import settings, logger


# --- Direcciones en Polygon (verificar antes de usar en LIVE) ---
# USDC.e (bridged) — el colateral que usa Polymarket.
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# CTF Exchange (mercados estándar).
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
# Neg-Risk Exchange (mercados multi-outcome / neg-risk).
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

USDC_DECIMALS = 6

# ABI mínimo ERC-20 para balance / allowance / approve.
_ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]

# Allowance "infinita" estándar para no re-aprobar en cada operación.
_MAX_UINT256 = 2 ** 256 - 1


class Wallet:
    def __init__(self, private_key: Optional[str] = None, rpc_url: Optional[str] = None) -> None:
        try:
            from web3 import Web3  # import perezoso
            from eth_account import Account
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "web3.py / eth-account no instalados. `pip install web3 eth-account`"
            ) from exc

        self._Web3 = Web3
        self.rpc_url = rpc_url or settings.polygon_rpc_url
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))

        pk = private_key or settings.polymarket_private_key
        if not pk:
            raise ValueError("POLYMARKET_PRIVATE_KEY no configurada.")
        self.account = Account.from_key(pk)
        self.address = self.account.address
        self.usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS), abi=_ERC20_ABI
        )

    # ------------------------------------------------------------------ #
    def conectado(self) -> bool:
        try:
            return bool(self.w3.is_connected())
        except Exception:  # pragma: no cover
            return False

    def balance_usdc(self) -> float:
        raw = self.usdc.functions.balanceOf(self.address).call()
        return raw / (10 ** USDC_DECIMALS)

    def balance_matic(self) -> float:
        raw = self.w3.eth.get_balance(self.address)
        return float(self._Web3.from_wei(raw, "ether"))

    def allowance(self, spender: str = CTF_EXCHANGE) -> float:
        spender = self._Web3.to_checksum_address(spender)
        raw = self.usdc.functions.allowance(self.address, spender).call()
        return raw / (10 ** USDC_DECIMALS)

    def asegurar_allowance(
        self, spender: str = CTF_EXCHANGE, minimo: Optional[float] = None
    ) -> Optional[str]:
        """Aprueba allowance de USDC al contrato si está por debajo del mínimo.

        Devuelve el hash de la transacción si aprueba, o None si no hizo falta.
        Requiere MATIC para gas. Solo debe usarse conscientemente en LIVE.
        """
        minimo = settings.capital_usdc if minimo is None else minimo
        actual = self.allowance(spender)
        if actual >= minimo:
            logger.info("Allowance suficiente para %s: %.2f USDC", spender, actual)
            return None

        spender_cs = self._Web3.to_checksum_address(spender)
        # Nonce "pending" para que aprobaciones consecutivas no colisionen
        # (evita 'replacement transaction underpriced' al encadenar varias).
        nonce = self.w3.eth.get_transaction_count(self.address, "pending")
        tx = self.usdc.functions.approve(spender_cs, _MAX_UINT256).build_transaction(
            {
                "from": self.address,
                "nonce": nonce,
                "maxFeePerGas": self.w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": self.w3.to_wei(30, "gwei"),
                "chainId": 137,  # Polygon mainnet
            }
        )
        firmada = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(firmada.raw_transaction)
        h = tx_hash.hex()
        logger.warning("Allowance enviada para %s en tx %s (esperando confirmación)", spender, h)
        # Esperar el recibo asegura que la siguiente tx use un nonce ya avanzado.
        try:
            self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            logger.warning("Allowance confirmada para %s", spender)
        except Exception as exc:  # noqa: BLE001 - la tx puede confirmar igual
            logger.warning("No se confirmó el recibo a tiempo (%s); revisa en Polygonscan.", exc)
        return h

    def resumen(self) -> dict:
        return {
            "address": self.address,
            "conectado": self.conectado(),
            "usdc": self.balance_usdc() if self.conectado() else None,
            "matic": self.balance_matic() if self.conectado() else None,
        }
