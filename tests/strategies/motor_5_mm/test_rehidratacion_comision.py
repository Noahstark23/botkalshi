"""
C1/6.3 — la rehidratación de inventario debe REPRODUCIR la comisión histórica, no recalcularla.

POR QUÉ EXISTE (encargo 22-sep, hallazgo 6.3): `_rehidratar_inventory()` reconstruye la
trayectoria de la cohorte llamando a `apply_fill(..., fee_multiplier=row.fee_multiplier or 1)`,
y `apply_fill` RECALCULA la comisión con el modo VIVO del engine (`_fees_as_maker`). La fila
ya guarda el hecho histórico en `fee_effective_cents` + `fee_model` (el productor los escribe
en `_persist_fill`, engine.py:1362-1378), y la recuperación los descarta. Dos defectos en una
línea:

  (a) `or 1` es una SUSTITUCIÓN SILENCIOSA: un `fee_multiplier` ausente (None) o cero
      (falsy) se convierte en 1 — el multiplicador de fee completo. Justo la clase de error
      que ya costó caro acá (la fee ~100× subestimada de 2026-07-01 invalidó meses de
      análisis) y el encargo lo prohíbe explícitamente: una comisión ausente o inconsistente
      BLOQUEA la recuperación, no la rellena.

  (b) Recalcular con el modo vivo REESCRIBE EL PASADO: una cohorte grabada como "taker"
      rehidratada por un engine con `fees_as_maker=True` recibe comisiones de maker
      retroactivas. Caja y comisión después de reiniciar dejan de coincidir con las de antes
      — que es exactamente el oráculo de aceptación "Reinicio: igual resultado que ejecución
      ininterrumpida".

Y hay un agravante concreto: el multiplicador maker de KXMLBGAME está EN DISPUTA (el código
afirma 0.5 desde 2026-08-07; el PDF oficial verificado el 13-ago dice 1). Mientras esa
pregunta siga abierta, recalcular comisiones en cada arranque es exactamente lo que no hay
que hacer — el hecho grabado es el único dato que no se mueve bajo nuestros pies.

El test existente `test_rehidrata_inventario_de_la_misma_cohorte` solo afirma
`net_contracts == 1`: nunca mira la comisión. Ese es el hueco que se cierra acá.
"""

from __future__ import annotations

import pytest

from src.storage.models import MMShadowFill, get_session
from src.strategies.motor_5_mm.engine import Motor5DataIntegrityError, Motor5Engine
from src.strategies.motor_5_mm.inventory import InventoryBook
from src.strategies.motor_5_mm.shadow_fill import ShadowFill

TICKER = "KXMLBGAME-E1-YES"


def _engine(**kw) -> Motor5Engine:
    base = {
        "experiment_label": "rehidratacion",
        "series_csv": "KXMLBGAME",
        "exchange_environment": "production",
    }
    base.update(kw)
    return Motor5Engine(**base)


def _persistir(eng: Motor5Engine, **campos) -> int:
    """Una fila de la cohorte del engine, con los campos de fee que el productor escribe."""
    fila = {
        "ticker": TICKER,
        "side": "buy",
        "price_cents": 47,
        "count": 1,
        "rule": "test",
        "inventory_after": 1,
        "metric_version": eng.F1_METRIC_VERSION,
        "experiment_id": eng._experiment_id,
    }
    fila.update(campos)
    with get_session() as s:
        row = MMShadowFill(**fila)
        s.add(row)
        s.commit()
        s.refresh(row)
        return int(row.id)


def test_rehidratacion_reproduce_la_comision_persistida():
    """La comisión rehidratada es la GRABADA, no una recalculada con el modo vivo.

    El valor persistido (999¢) no lo produce ninguna de las dos fórmulas a 47¢/1 contrato:
    si aparece en el inventario, la recuperación replicó el hecho histórico en vez de
    inventarlo de nuevo. No es circular — no se compara contra la función que se audita."""
    eng = _engine(fees_as_maker=True)  # modo VIVO maker…
    _persistir(
        eng,
        fee_model="taker",  # …sobre una cohorte grabada como TAKER
        fee_effective_cents=999,
        fee_multiplier=1.0,
    )

    eng._rehidratar_inventory()

    inv = eng._inventory.positions[TICKER]
    assert inv.fees_cents == 999, (
        "La rehidratación recalculó la comisión con el modo vivo en vez de reproducir "
        f"fee_effective_cents (obtuvo {inv.fees_cents}¢). Eso reescribe el pasado."
    )


def test_comision_ausente_bloquea_la_recuperacion():
    """`fee_effective_cents` NULL no puede volverse un default: bloquea y nombra la fila.

    Una cohorte sin evidencia suficiente se identifica y se separa — no se 'arregla'
    rellenando. El error tiene que decir QUÉ fila, para poder auditarla."""
    eng = _engine(fees_as_maker=True)
    fila_id = _persistir(eng, fee_model="taker", fee_effective_cents=None, fee_multiplier=1.0)

    with pytest.raises(Motor5DataIntegrityError) as exc:
        eng._rehidratar_inventory()
    assert str(fila_id) in str(exc.value), "El bloqueo no identifica la fila que lo causó"


def test_multiplicador_cero_no_se_convierte_en_uno():
    """El bug clásico de `or`: 0.0 es falsy y se volvía 1 — el multiplicador COMPLETO.

    Un cero legítimo (serie sin comisión) pasaba a pagar fee entera, y un cero corrupto
    pasaba inadvertido. Con la comisión grabada mandando, el multiplicador ya no decide
    nada en la recuperación y el valor persistido se respeta tal cual."""
    eng = _engine(fees_as_maker=True)
    _persistir(eng, fee_model="maker", fee_effective_cents=0, fee_multiplier=0.0)

    eng._rehidratar_inventory()

    assert eng._inventory.positions[TICKER].fees_cents == 0


def test_reinicio_reproduce_caja_posicion_y_comision():
    """ORÁCULO DE ACEPTACIÓN: reiniciar da el MISMO estado que no haber reiniciado.

    Se construye la trayectoria en vivo, se graba lo que el productor grabaría, y se
    rehidrata en un engine nuevo: caja, posición y comisión tienen que coincidir las tres.
    Comparar solo la posición (lo que hacía el test previo) deja pasar el defecto entero."""
    vivo = InventoryBook(fees_as_maker=False)  # cohorte TAKER
    inv_vivo = vivo.apply_fill(
        ShadowFill(ticker=TICKER, side="buy", price_cents=47, count=1, rule="test"),
        fee_multiplier=1,
    )

    eng = _engine(fees_as_maker=True)  # el engine que reinicia está en modo maker
    _persistir(
        eng,
        fee_model="taker",
        fee_effective_cents=inv_vivo.fees_cents,
        fee_multiplier=1.0,
    )

    eng._rehidratar_inventory()
    inv_rehidratado = eng._inventory.positions[TICKER]

    assert inv_rehidratado.net_contracts == inv_vivo.net_contracts
    assert inv_rehidratado.cash_cents == inv_vivo.cash_cents
    assert inv_rehidratado.fees_cents == inv_vivo.fees_cents, (
        "La comisión cambió al reiniciar: el estado post-reinicio no reproduce el previo"
    )


def test_cohorte_ajena_no_entra_en_la_recuperacion():
    """CONTROL: el filtro por cohorte sigue vivo — una fila de otro experiment_id no suma.

    Sin este control, un fix del fee podría 'arreglar' la comisión mezclando cohortes."""
    eng = _engine(fees_as_maker=True)
    with get_session() as s:
        s.add(
            MMShadowFill(
                ticker=TICKER,
                side="buy",
                price_cents=47,
                count=1,
                rule="test",
                inventory_after=1,
                metric_version=eng.F1_METRIC_VERSION,
                experiment_id="otra-cohorte",
                fee_model="taker",
                fee_effective_cents=999,
                fee_multiplier=1.0,
            )
        )
        s.commit()

    eng._rehidratar_inventory()

    assert eng._inventory.net(TICKER) == 0
    assert TICKER not in eng._inventory.positions
