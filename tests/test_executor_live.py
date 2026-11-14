"""
tests/test_executor_live.py
==========================
Pruebas de la ORQUESTACIÓN del camino LIVE (sin red ni clave real):

    - Arbitraje cubierto (compra SÍ y NO -> ganancia en cualquier desenlace).
    - Leg risk: si la 2ª pata no se llena, se deshace la 1ª.
    - Leg risk severo: si tampoco se puede deshacer -> DESBALANCEADA.
    - Preflight: balance/allowance insuficiente -> no se envía nada.
    - Revalidación: si el arbitraje deja de ser rentable a precios ejecutables,
      no se opera.

Se usa un cliente y un wallet falsos; `_registrar_live` se intercepta para no
tocar la base de datos.

Ejecutar:  pytest tests/test_executor_live.py -v
"""

import pytest

from config.settings import settings
from core.executor import Executor
from core.math_engine import (
    TIPO_ARBITRAJE_SUMA,
    TIPO_ARBITRAJE_MULTIPLE,
    FORMA_LAY_FIELD,
)


class FakeWallet:
    def __init__(self, balance=1_000_000.0, allowance=1_000_000.0):
        self._b = balance
        self._a = allowance

    def balance_usdc(self):
        return self._b

    def allowance(self, spender=None):
        return self._a


class MockClient:
    """Cliente de Polymarket falso con orderbook y plan de fills configurables."""

    def __init__(self, asks, bids, fill_plan):
        self.asks = asks
        self.bids = bids
        self.fill_plan = fill_plan  # callable(token, side) -> bool
        self.calls = []

    def best_ask(self, token):
        return self.asks.get(token)

    def best_bid(self, token):
        return self.bids.get(token)

    def place_order(self, token, side, price, size, order_type="FOK", market_id=None):
        self.calls.append((token, side.upper(), order_type.upper()))
        filled = self.fill_plan(token, side.upper())
        return {"filled": filled, "status": "matched" if filled else "unmatched"}


@pytest.fixture(autouse=True)
def forzar_live(monkeypatch):
    # Activa el modo live (PHASE=3 + LIVE_TRADING=true) solo para estos tests.
    monkeypatch.setattr(settings, "live_trading", True)
    monkeypatch.setattr(settings, "phase", 3)
    assert settings.es_live is True


def _decision_arb():
    return {
        "tipo": TIPO_ARBITRAJE_SUMA,
        "market_id": "M",
        "titulo": "Mercado de prueba",
        "precio_si": 0.45,
        "precio_no": 0.45,
        "apuesta_si": 50.0,
        "apuesta_no": 50.0,
        "ganancia_minima_usd": 5.0,
        "clob_token_ids": ["TSI", "TNO"],
    }


def _ejecutor(mock):
    ex = Executor(client=mock, wallet=FakeWallet())
    # Interceptar el registro en DB: solo capturamos el estado final.
    ex._registro = []
    ex._registrar_live = lambda decision, total, estado, modo="live": (
        ex._registro.append(estado) or estado
    )
    return ex


def test_arbitraje_cubierto_ambos_lados():
    mock = MockClient(
        asks={"TSI": 0.45, "TNO": 0.45},
        bids={"TSI": 0.44, "TNO": 0.44},
        fill_plan=lambda token, side: True,  # todo se llena
    )
    ex = _ejecutor(mock)
    ex._live_arbitraje(_decision_arb())
    assert ex._registro[-1] == "EJECUTADA"
    # Se compraron AMBOS lados (SÍ y NO).
    compras = [c for c in mock.calls if c[1] == "BUY"]
    assert ("TSI", "BUY", "FOK") in compras
    assert ("TNO", "BUY", "FOK") in compras


def test_leg_risk_deshace_primera_pata():
    # La pata NO no se llena; la venta de deshacer SÍ sí se llena.
    def plan(token, side):
        if token == "TNO" and side == "BUY":
            return False
        return True

    mock = MockClient(
        asks={"TSI": 0.45, "TNO": 0.45},
        bids={"TSI": 0.44, "TNO": 0.44},
        fill_plan=plan,
    )
    ex = _ejecutor(mock)
    ex._live_arbitraje(_decision_arb())
    assert ex._registro[-1] == "CERRADA"  # deshecho correctamente
    assert ("TSI", "SELL", "FOK") in mock.calls  # se intentó deshacer


def test_leg_risk_desbalanceada_si_no_se_puede_deshacer():
    # NO no se llena y la venta de SÍ tampoco -> posición desbalanceada.
    def plan(token, side):
        if token == "TNO" and side == "BUY":
            return False
        if token == "TSI" and side == "SELL":
            return False
        return True

    mock = MockClient(
        asks={"TSI": 0.45, "TNO": 0.45},
        bids={"TSI": 0.44, "TNO": 0.44},
        fill_plan=plan,
    )
    from core.risk import risk_manager

    risk_manager.reanudar()  # asegurar estado limpio antes
    ex = _ejecutor(mock)
    ex._live_arbitraje(_decision_arb())
    assert ex._registro[-1] == "DESBALANCEADA"
    # Una posición sin cubrir debe pausar el bot de inmediato.
    assert risk_manager.pausado is True
    risk_manager.reanudar()  # limpiar para no afectar a otros tests


def test_preflight_balance_insuficiente_no_opera():
    mock = MockClient(
        asks={"TSI": 0.45, "TNO": 0.45},
        bids={"TSI": 0.44, "TNO": 0.44},
        fill_plan=lambda token, side: True,
    )
    ex = Executor(client=mock, wallet=FakeWallet(balance=10.0))  # < 100 total
    ex._registro = []
    ex._registrar_live = lambda d, t, e: ex._registro.append(e)
    res = ex._live_arbitraje(_decision_arb())
    assert res is None
    assert mock.calls == []  # no se envió ninguna orden


def test_dry_run_no_envia_pero_revalida(monkeypatch):
    # Dry-run: corre preflight + revalidación de orderbook pero NO envía órdenes.
    monkeypatch.setattr(settings, "dry_run", True)
    mock = MockClient(
        asks={"TSI": 0.45, "TNO": 0.45},
        bids={"TSI": 0.44, "TNO": 0.44},
        fill_plan=lambda token, side: True,
    )
    ex = _ejecutor(mock)
    ex._live_arbitraje(_decision_arb())
    assert ex._registro[-1] == "DRY_RUN"
    assert mock.calls == []  # ninguna orden enviada


def test_dry_run_aborta_si_preflight_falla(monkeypatch):
    # En dry-run el preflight sigue siendo real: balance insuficiente -> aborta.
    monkeypatch.setattr(settings, "dry_run", True)
    mock = MockClient(
        asks={"TSI": 0.45, "TNO": 0.45},
        bids={"TSI": 0.44, "TNO": 0.44},
        fill_plan=lambda token, side: True,
    )
    ex = Executor(client=mock, wallet=FakeWallet(balance=10.0))
    ex._registro = []
    ex._registrar_live = lambda d, t, e, modo="live": ex._registro.append(e)
    res = ex._live_arbitraje(_decision_arb())
    assert res is None
    assert mock.calls == []


def _decision_multi():
    return {
        "tipo": TIPO_ARBITRAJE_MULTIPLE,
        "forma": FORMA_LAY_FIELD,
        "n": 3,
        "market_id": "E",
        "titulo": "Evento de prueba",
        "capital_comprometido": 90.0,
        "ganancia_minima_usd": 7.5,
        "legs": [
            {"token_id": "A", "side": "BUY", "price": 0.60, "shares": 50.0},
            {"token_id": "B", "side": "BUY", "price": 0.60, "shares": 50.0},
            {"token_id": "C", "side": "BUY", "price": 0.60, "shares": 50.0},
        ],
    }


def _book_multi():
    asks = {"A": 0.60, "B": 0.60, "C": 0.60}
    bids = {"A": 0.59, "B": 0.59, "C": 0.59}
    return asks, bids


def test_multi_cubierto_todas_las_patas():
    asks, bids = _book_multi()
    mock = MockClient(asks, bids, fill_plan=lambda t, s: True)
    ex = _ejecutor(mock)
    ex._live_multiple(_decision_multi())
    assert ex._registro[-1] == "EJECUTADA"
    compras = [c for c in mock.calls if c[1] == "BUY"]
    assert len(compras) == 3  # una por resultado


def test_multi_leg_risk_deshace_si_falla_una_pata():
    asks, bids = _book_multi()
    # Falla la compra de C; las ventas de deshacer (A, B) sí se llenan.
    def plan(token, side):
        if token == "C" and side == "BUY":
            return False
        return True

    mock = MockClient(asks, bids, fill_plan=plan)
    from core.risk import risk_manager
    risk_manager.reanudar()
    ex = _ejecutor(mock)
    ex._live_multiple(_decision_multi())
    assert ex._registro[-1] == "CERRADA"  # se deshicieron A y B
    assert any(c[1] == "SELL" for c in mock.calls)
    risk_manager.reanudar()


def test_multi_dry_run_no_envia(monkeypatch):
    monkeypatch.setattr(settings, "dry_run", True)
    asks, bids = _book_multi()
    mock = MockClient(asks, bids, fill_plan=lambda t, s: True)
    ex = _ejecutor(mock)
    ex._live_multiple(_decision_multi())
    assert ex._registro[-1] == "DRY_RUN"
    assert mock.calls == []


def test_multi_no_opera_si_deja_de_ser_rentable():
    # Asks altos -> Σ ejecutable ~ n-1 -> sin margen -> None.
    asks = {"A": 0.67, "B": 0.67, "C": 0.67}
    bids = {"A": 0.66, "B": 0.66, "C": 0.66}
    mock = MockClient(asks, bids, fill_plan=lambda t, s: True)
    ex = _ejecutor(mock)
    res = ex._live_multiple(_decision_multi())
    assert res is None
    assert mock.calls == []


def test_no_opera_si_arbitraje_deja_de_ser_rentable():
    # asks altos -> suma ejecutable >= umbral -> no se opera.
    mock = MockClient(
        asks={"TSI": 0.49, "TNO": 0.49},
        bids={"TSI": 0.48, "TNO": 0.48},
        fill_plan=lambda token, side: True,
    )
    ex = _ejecutor(mock)
    res = ex._live_arbitraje(_decision_arb())
    assert res is None
    assert mock.calls == []  # ni siquiera se intentó
