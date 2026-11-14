"""
tests/test_feeds.py
==================
Resiliencia del cliente ante tokens sin orderbook (un 404 no debe romper nada).

Ejecutar:  pytest tests/test_feeds.py -v
"""

import json

from feeds.polymarket import PolymarketClient


def _evento(neg_risk, n=3):
    return {
        "id": "E", "title": "t", "negRisk": neg_risk,
        "markets": [
            {
                "id": f"m{i}", "question": f"q{i}",
                "outcomePrices": json.dumps(["0.30", "0.70"]),
                "clobTokenIds": json.dumps([f"Y{i}", f"N{i}"]),
            }
            for i in range(n)
        ],
    }


def test_evento_no_negrisk_se_descarta():
    # Sin exclusión mutua (umbrales anidados tipo "FDV > X") -> None.
    assert PolymarketClient._normalizar_evento(_evento(False), 2, 8) is None


def test_evento_negrisk_se_normaliza():
    norm = PolymarketClient._normalizar_evento(_evento(True), 2, 8)
    assert norm is not None
    assert len(norm["outcomes"]) == 3
    assert norm["neg_risk"] is True
    assert norm["outcomes"][0]["token_no"] == "N0"


def test_get_orderbook_devuelve_vacio_ante_error(monkeypatch):
    c = PolymarketClient()

    def boom(url, params=None):
        raise RuntimeError("404 Not Found")

    monkeypatch.setattr(c, "_get", boom)
    ob = c.get_orderbook("token_sin_libro")
    assert ob["bids"] == []
    assert ob["asks"] == []
    assert ob["liquidez_ask"] == 0.0


def test_best_ask_bid_none_sin_libro(monkeypatch):
    c = PolymarketClient()
    monkeypatch.setattr(
        c, "get_orderbook",
        lambda token: {"market_id": token, "bids": [], "asks": []},
    )
    assert c.best_ask("tok") is None
    assert c.best_bid("tok") is None


def test_best_ask_toma_el_menor(monkeypatch):
    c = PolymarketClient()
    monkeypatch.setattr(
        c, "get_orderbook",
        lambda token: {
            "bids": [{"price": "0.40"}, {"price": "0.42"}],
            "asks": [{"price": "0.55"}, {"price": "0.50"}],
        },
    )
    assert c.best_ask("tok") == 0.50  # menor ask
    assert c.best_bid("tok") == 0.42  # mayor bid
