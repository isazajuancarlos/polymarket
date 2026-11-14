"""
tests/test_math_engine.py
=========================
Pruebas del motor matemático. Inyectamos los umbrales en el constructor para
que los tests sean deterministas e independientes del .env.

Ejecutar:  pytest tests/test_math_engine.py -v
"""

import pytest

from core.math_engine import (
    MotorMatematico,
    MercadoView,
    TIPO_ARBITRAJE_SUMA,
    TIPO_ARBITRAJE_MULTIPLE,
    TIPO_EV_POSITIVO,
    TIPO_MARKET_MAKING,
    FORMA_LAY_FIELD,
    FORMA_BUY_ALL_YES,
)


def _outcome(yes, no, i):
    return {"yes_ask": yes, "no_ask": no, "token_yes": f"Y{i}", "token_no": f"N{i}",
            "titulo": f"out{i}"}


@pytest.fixture
def motor():
    return MotorMatematico(
        fee=0.02,
        arb_threshold=0.97,
        kelly_fraction=0.25,
        max_position_pct=0.10,
        min_ev=0.05,
        min_liquidity=200.0,
        mm_min_volume=5000.0,
        mm_half_spread=0.03,
        mm_min_edge=0.005,
        mm_skew=0.5,
        ev_enabled=True,
        ev_min_divergence=0.10,
    )


# --------------------------------------------------------------------------- #
# Arbitraje de suma
# --------------------------------------------------------------------------- #
def test_arbitraje_detecta_oportunidad(motor):
    # suma = 0.90 < 0.97 -> hay arbitraje
    res = motor.calcular_arbitraje_suma(0.45, 0.45, 100.0)
    assert res is not None
    assert res["tipo"] == TIPO_ARBITRAJE_SUMA
    assert res["suma"] == pytest.approx(0.90)
    # ganancia_pct = (1 - 0.90 - 0.02) / 0.90 = 0.08/0.90
    assert res["ganancia_garantizada_pct"] == pytest.approx(0.08 / 0.90)


def test_arbitraje_hedge_perfecto(motor):
    # El número de shares de cada lado debe ser idéntico => payout garantizado.
    capital = 100.0
    res = motor.calcular_arbitraje_suma(0.40, 0.55, capital)
    assert res is not None
    suma = 0.95
    shares_si = res["apuesta_si"] / 0.40
    shares_no = res["apuesta_no"] / 0.55
    assert shares_si == pytest.approx(shares_no)
    assert shares_si == pytest.approx(capital / suma)
    # Las apuestas suman el capital.
    assert res["apuesta_si"] + res["apuesta_no"] == pytest.approx(capital)


def test_arbitraje_no_oportunidad_si_suma_alta(motor):
    # suma = 0.98 >= 0.97 -> None
    assert motor.calcular_arbitraje_suma(0.49, 0.49, 100.0) is None


def test_arbitraje_no_si_fee_consume_ganancia(motor):
    # suma = 0.965 -> (1 - 0.965 - 0.02)/0.965 = 0.015/0.965 > 0 (sí rentable)
    res = motor.calcular_arbitraje_suma(0.48, 0.485, 100.0)
    assert res is not None
    # suma = 0.979 >= umbral -> None (ni siquiera se evalúa la fee)
    assert motor.calcular_arbitraje_suma(0.49, 0.489, 100.0) is None


def test_arbitraje_entradas_invalidas(motor):
    assert motor.calcular_arbitraje_suma(0.0, 0.5, 100.0) is None
    assert motor.calcular_arbitraje_suma(0.5, 0.5, 0.0) is None


# --------------------------------------------------------------------------- #
# EV
# --------------------------------------------------------------------------- #
def test_ev_positivo(motor):
    # prob 0.6, precio 0.5 => b=1, ev = 0.6*1 - 0.4 = 0.2
    assert motor.calcular_ev(0.6, 0.5) == pytest.approx(0.2)


def test_ev_negativo(motor):
    # prob 0.4, precio 0.5 => ev = 0.4 - 0.6 = -0.2
    assert motor.calcular_ev(0.4, 0.5) == pytest.approx(-0.2)


def test_ev_precio_invalido(motor):
    assert motor.calcular_ev(0.6, 0.0) == -1.0
    assert motor.calcular_ev(0.6, 1.0) == -1.0


# --------------------------------------------------------------------------- #
# Kelly
# --------------------------------------------------------------------------- #
def test_kelly_basico(motor):
    # prob 0.6, precio 0.5 => b=1, f* = (0.6*1 - 0.4)/1 = 0.2
    # f_conservador = 0.2 * 0.25 = 0.05 ; min(0.05, 0.10) = 0.05
    apuesta = motor.kelly_criterion(0.6, 0.5, 1000.0)
    assert apuesta == pytest.approx(50.0)


def test_kelly_cap_por_max_position(motor):
    # Una ventaja enorme debe quedar limitada al 10% del capital.
    apuesta = motor.kelly_criterion(0.95, 0.5, 1000.0)
    assert apuesta == pytest.approx(100.0)  # 10% de 1000


def test_kelly_no_apuesta_si_negativo(motor):
    # f* negativo -> 0
    assert motor.kelly_criterion(0.3, 0.5, 1000.0) == 0.0


def test_kelly_precio_invalido(motor):
    assert motor.kelly_criterion(0.6, 0.0, 1000.0) == 0.0
    assert motor.kelly_criterion(0.6, 1.0, 1000.0) == 0.0


# --------------------------------------------------------------------------- #
# Arbitraje multi-resultado
# --------------------------------------------------------------------------- #
def test_multi_lay_field_detecta_sobrevalorado(motor):
    # 3 resultados, NO a 0.60 c/u => Σno=1.8 < n-1=2 => lay rentable.
    outcomes = [_outcome(0.40, 0.60, i) for i in range(3)]
    res = motor.calcular_arbitraje_multiple(outcomes, capital=100.0)
    assert res is not None
    assert res["tipo"] == TIPO_ARBITRAJE_MULTIPLE
    assert res["forma"] == FORMA_LAY_FIELD
    assert res["n"] == 3
    assert len(res["legs"]) == 3
    # profit = (n-1)/Σno - 1 - fee = 2/1.8 - 1 - 0.02
    assert res["profit_pct"] == pytest.approx(2 / 1.8 - 1 - 0.02)
    # Mismo nº de shares por pata y todas compran NO.
    assert all(l["outcome"] == "NO" for l in res["legs"])
    assert all(l["shares"] == pytest.approx(res["shares"]) for l in res["legs"])


def test_multi_sin_arbitraje_si_eficiente(motor):
    # Σno = n-1 exacto => sin margen tras fee => None.
    outcomes = [_outcome(1 / 3, 2 / 3, i) for i in range(3)]
    assert motor.calcular_arbitraje_multiple(outcomes, capital=100.0) is None


def test_multi_buy_all_yes_solo_si_exhaustivo(motor):
    # Σyes=0.90<1 (underround) pero Σno=2.1>n-1 (lay no sirve).
    outcomes = [_outcome(0.30, 0.70, i) for i in range(3)]
    # Sin exhaustividad: ninguna forma válida.
    assert motor.calcular_arbitraje_multiple(outcomes, capital=100.0) is None
    # Con exhaustividad: buy-all-yes.
    res = motor.calcular_arbitraje_multiple(outcomes, capital=100.0, exhaustivo=True)
    assert res is not None
    assert res["forma"] == FORMA_BUY_ALL_YES
    assert all(l["outcome"] == "SI" for l in res["legs"])


def test_multi_respeta_max_outcomes(motor):
    outcomes = [_outcome(0.40, 0.60, i) for i in range(5)]
    assert motor.calcular_arbitraje_multiple(
        outcomes, capital=100.0, max_outcomes=4
    ) is None


def test_multi_ganancia_proporcional_al_capital(motor):
    outcomes = [_outcome(0.40, 0.60, i) for i in range(3)]
    res = motor.calcular_arbitraje_multiple(outcomes, capital=200.0)
    assert res["ganancia_minima_usd"] == pytest.approx(200.0 * res["profit_pct"])


# --------------------------------------------------------------------------- #
# Market making
# --------------------------------------------------------------------------- #
def test_mm_genera_cotizaciones(motor):
    # fair 0.50, half_spread 0.03 => bid 0.47, ask 0.53, spread 0.06.
    res = motor.calcular_market_making(
        fair_value=0.50, volumen=10000.0, capital=1000.0, inventario_shares=0.0
    )
    assert res is not None
    assert res["tipo"] == TIPO_MARKET_MAKING
    assert res["bid"] == pytest.approx(0.47)
    assert res["ask"] == pytest.approx(0.53)
    assert res["spread"] == pytest.approx(0.06)
    # edge = spread - fee*(bid+ask) = 0.06 - 0.02*1.0 = 0.04
    assert res["edge_neto"] == pytest.approx(0.04)
    assert res["bid"] < res["fair_value"] < res["ask"]


def test_mm_descarta_bajo_volumen(motor):
    # volumen < mm_min_volume => None
    assert motor.calcular_market_making(0.50, 1000.0, 1000.0) is None


def test_mm_descarta_si_edge_insuficiente(motor):
    # Con half_spread minúsculo el edge no cubre fees => None.
    estrecho = MotorMatematico(
        fee=0.02, mm_min_volume=5000.0, mm_half_spread=0.005,
        mm_min_edge=0.005, mm_skew=0.5, max_position_pct=0.10,
    )
    assert estrecho.calcular_market_making(0.50, 10000.0, 1000.0) is None


def test_mm_skew_por_inventario(motor):
    # Inventario largo (+shares) debe bajar las cotizaciones para vender.
    base = motor.calcular_market_making(0.50, 10000.0, 1000.0, inventario_shares=0.0)
    largo = motor.calcular_market_making(
        0.50, 10000.0, 1000.0, inventario_shares=10_000.0  # inventario muy largo
    )
    assert largo["bid"] < base["bid"]
    assert largo["ask"] < base["ask"]


# --------------------------------------------------------------------------- #
# decision_final (prioridades)
# --------------------------------------------------------------------------- #
def test_decision_prioriza_arbitraje(motor):
    mercado = MercadoView(
        precio_si=0.45, precio_no=0.45, liquidez=500.0, prob_modelo=0.9
    )
    decision = motor.decision_final(mercado, capital=1000.0)
    assert decision is not None
    assert decision["tipo"] == TIPO_ARBITRAJE_SUMA


def test_decision_arbitraje_requiere_liquidez(motor):
    # Sin liquidez suficiente, el arbitraje no aplica; cae a EV si lo hay.
    mercado = MercadoView(
        precio_si=0.45, precio_no=0.45, liquidez=50.0, prob_modelo=None
    )
    assert motor.decision_final(mercado, capital=1000.0) is None


def test_decision_ev_cuando_no_hay_arbitraje(motor):
    # suma alta => sin arbitraje; prob alta => EV positivo.
    mercado = MercadoView(
        precio_si=0.50, precio_no=0.50, liquidez=500.0, prob_modelo=0.65
    )
    decision = motor.decision_final(mercado, capital=1000.0)
    assert decision is not None
    assert decision["tipo"] == TIPO_EV_POSITIVO
    assert decision["ev"] > 0.05
    assert decision["apuesta"] > 0


def test_decision_none_sin_oportunidad(motor):
    # suma alta, prob = precio => EV ~ 0
    mercado = MercadoView(
        precio_si=0.50, precio_no=0.50, liquidez=500.0, prob_modelo=0.50
    )
    assert motor.decision_final(mercado, capital=1000.0) is None


def test_decision_ev_desactivado_si_ev_enabled_false():
    # Con EV deshabilitado, ni con prob fuerte se opera EV.
    motor = MotorMatematico(
        fee=0.02, arb_threshold=0.97, kelly_fraction=0.25, max_position_pct=0.10,
        min_ev=0.05, min_liquidity=200.0, ev_enabled=False, ev_min_divergence=0.10,
    )
    mercado = MercadoView(
        precio_si=0.50, precio_no=0.50, liquidez=500.0, prob_modelo=0.70
    )
    assert motor.decision_final(mercado, capital=1000.0) is None


def test_decision_ev_requiere_divergencia_minima(motor):
    # prob 0.55 vs precio 0.50 -> divergencia 0.05 < 0.10 -> no opera aunque EV>0.
    mercado = MercadoView(
        precio_si=0.50, precio_no=0.50, liquidez=500.0, prob_modelo=0.55
    )
    assert motor.decision_final(mercado, capital=1000.0) is None
