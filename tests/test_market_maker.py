"""
tests/test_market_maker.py
=========================
Pruebas de los filtros de calidad del market maker (sin red ni DB):

    - Cotiza en un mercado sano (libro de dos lados, fair en banda).
    - NO cotiza si el fair está fuera de banda (longshot/casi-seguro).
    - NO cotiza si el libro es de un solo lado.
    - NO cotiza si el volumen es bajo.

Ejecutar:  pytest tests/test_market_maker.py -v
"""

import pytest

from core.market_maker import MarketMaker
from core.math_engine import MotorMatematico, TIPO_MARKET_MAKING
import core.risk as risk_mod
from core.risk import ResultadoRiesgo


class FakeBook:
    def __init__(self, bid, ask):
        self.bid = bid
        self.ask = ask
        self.orders = []

    def best_bid(self, token):
        return self.bid

    def best_ask(self, token):
        return self.ask

    def place_order(self, token, side, price, size, order_type="GTC", market_id=None):
        self.orders.append((side.upper(), round(price, 3)))
        return {"filled": True}


@pytest.fixture(autouse=True)
def riesgo(monkeypatch):
    risk_mod.risk_manager.reanudar()  # estado limpio
    monkeypatch.setattr(
        risk_mod.risk_manager, "reporte_exposicion",
        lambda: {"capital_actual": 1000.0},
    )
    monkeypatch.setattr(
        risk_mod.risk_manager, "puede_operar",
        lambda monto: ResultadoRiesgo(True, "ok", monto_ajustado=monto),
    )


def _mm(client):
    mm = MarketMaker(client=client, motor=MotorMatematico())
    mm._registrar = lambda c: None  # no tocar DB
    return mm


def _mercado(volumen=10000.0):
    return {
        "market_id": "M", "titulo": "Mercado sano",
        "volumen": volumen, "clob_token_ids": ["TSI", "TNO"],
    }


def test_cotiza_mercado_sano():
    client = FakeBook(bid=0.48, ask=0.52)  # fair 0.50, en banda
    mm = _mm(client)
    c = mm.cotizar_mercado(_mercado())
    assert c is not None
    assert c["tipo"] == TIPO_MARKET_MAKING
    assert len(client.orders) == 2  # BUY (bid) + SELL (ask)


def test_no_cotiza_fuera_de_banda():
    # fair 0.03 -> longshot, trampa de inventario. (el caso "Will Jesus return?")
    client = FakeBook(bid=0.02, ask=0.04)
    mm = _mm(client)
    assert mm.cotizar_mercado(_mercado()) is None
    assert client.orders == []


def test_no_cotiza_libro_de_un_lado():
    client = FakeBook(bid=0.48, ask=None)  # sin ask: no hay mercado que hacer
    mm = _mm(client)
    assert mm.cotizar_mercado(_mercado()) is None
    assert client.orders == []


def test_no_cotiza_bajo_volumen():
    client = FakeBook(bid=0.48, ask=0.52)
    mm = _mm(client)
    assert mm.cotizar_mercado(_mercado(volumen=1000.0)) is None
    assert client.orders == []
