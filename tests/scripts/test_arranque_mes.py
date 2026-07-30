"""
scripts/arranque_mes.py — arranque del mes en un comando, con gatillo HUMANO.

El script existe para que el operador no tenga que hacer la coreografía manual (mirar
/status, contar posiciones, correr clear_kill_switch, revisar 20 flags a ojo). Lo que se
verifica acá es lo que lo hace seguro:

  - Sin `--arrancar` es READ-ONLY ABSOLUTO: no limpia el kill-switch pase lo que pase.
  - Con `--arrancar` exige la confirmación tipeada EXACTA.
  - Aborta si hay posiciones abiertas, y también si NO PUDO leerlas (fail-closed: 'no sé'
    no habilita nada — el caso del contrato COLSD del 2026-07-29).
  - Reporta los flags faltantes con el valor esperado, listo para pegar en Coolify
    (incluye el caso 'variable ausente → aplica el default de Pydantic', la trampa que ya
    hizo concluir "está hardcodeado" cuando no lo estaba).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scripts import arranque_mes


@pytest.fixture
def settings_del_mes() -> MagicMock:
    """Settings con TODOS los flags del mes ya aplicados."""
    s = MagicMock()
    for nombre, esperado in arranque_mes.FLAGS_DEL_MES.items():
        setattr(s, nombre, esperado)
    s.CAPITAL_FLOOR_USD = 100.0
    return s


def _run(
    argv, *, settings, posiciones, engaged, capital=None, feed=None, pausa=None
) -> tuple[int, str]:
    capital = capital if capital is not None else {"effective_usd": 270.0, "is_paused": False}
    feed = feed if feed is not None else {"books_initialized": 215}
    # Default: bot vivo sin pausa runtime (el punto ciego del 2026-07-30 se testea aparte).
    pausa = (
        pausa
        if pausa is not None
        else {"is_paused": False, "pause_reason": None, "verificable": True}
    )
    with (
        patch.object(arranque_mes, "get_settings", return_value=settings),
        patch.object(arranque_mes, "_leer_posiciones", MagicMock(return_value=posiciones)),
        patch.object(arranque_mes, "_estado_capital", return_value=capital),
        patch.object(arranque_mes, "_estado_pausa_runtime", return_value=pausa),
        patch.object(arranque_mes, "_estado_feed", return_value=feed),
        patch.object(arranque_mes.models, "kill_switch_engaged", return_value=engaged),
        patch.object(arranque_mes.models, "clear_kill_switch") as clear,
        patch("builtins.print") as prt,
    ):
        rc = arranque_mes.main(argv)
        salida = "\n".join(str(c.args[0]) if c.args else "" for c in prt.call_args_list)
    return rc, salida, clear


# =====================================================
# Modo CHEQUEO: read-only absoluto
# =====================================================


def test_chequeo_nunca_limpia_el_kill_switch(settings_del_mes):
    """LA garantía del modo default: aunque TODO esté listo, sin --arrancar no muta nada."""
    rc, salida, clear = _run(
        [], settings=settings_del_mes, posiciones=[], engaged=(True, "rollback_aborted_slippage")
    )
    assert rc == 0
    clear.assert_not_called()
    assert "read-only" in salida.lower()


def test_chequeo_reporta_flags_faltantes_con_valor_esperado(settings_del_mes):
    """El output tiene que ser PEGABLE en Coolify: NOMBRE=valor + el actual como comentario."""
    settings_del_mes.MOTOR_2_MIN_EDGE_PCT = 3.0  # el default de Pydantic, sin setear en el env
    settings_del_mes.TRADING_ENABLED = False
    rc, salida, _ = _run([], settings=settings_del_mes, posiciones=[], engaged=(False, None))

    assert rc == 0
    assert "MOTOR_2_MIN_EDGE_PCT=1.0" in salida
    assert "actual: 3.0" in salida
    assert "TRADING_ENABLED=true" in salida  # bool en minúscula, formato .env


def test_chequeo_todo_listo_lo_dice(settings_del_mes):
    rc, salida, _ = _run([], settings=settings_del_mes, posiciones=[], engaged=(False, None))
    assert rc == 0
    assert "todo listo" in salida.lower()


def test_chequeo_avisa_capital_bajo_el_piso(settings_del_mes):
    """El mes no arranca si el cash está bajo el piso, por más flags que estén en true."""
    rc, salida, _ = _run(
        [],
        settings=settings_del_mes,
        posiciones=[],
        engaged=(False, None),
        capital={"effective_usd": 100.0, "raw_balance_usd": 99.16, "is_paused": True},
    )
    assert "capital bajo el piso" in salida.lower()


# =====================================================
# Modo ARRANCAR: gatillo humano + fail-closed
# =====================================================


def test_arrancar_exige_confirmacion_exacta(settings_del_mes):
    """Cualquier cosa que no sea la frase exacta cancela — incluido un 'si' apurado."""
    with patch("builtins.input", return_value="si dale"):
        rc, salida, clear = _run(
            ["--arrancar"],
            settings=settings_del_mes,
            posiciones=[],
            engaged=(True, "rollback_aborted_slippage"),
        )
    assert rc == 1
    clear.assert_not_called()
    assert "cancelado" in salida.lower()


def test_arrancar_con_confirmacion_limpia(settings_del_mes):
    with patch("builtins.input", return_value=arranque_mes.CONFIRMACION):
        rc, salida, clear = _run(
            ["--arrancar"],
            settings=settings_del_mes,
            posiciones=[],
            engaged=(True, "rollback_aborted_slippage"),
        )
    assert rc == 0
    clear.assert_called_once()
    assert "limpiado" in salida.lower()


def test_arrancar_aborta_con_posiciones_abiertas(settings_del_mes):
    """El caso del 2026-07-29: la pata huérfana de COLSD. No se limpia con exposición viva."""
    with patch("builtins.input", return_value=arranque_mes.CONFIRMACION) as inp:
        rc, salida, clear = _run(
            ["--arrancar"],
            settings=settings_del_mes,
            posiciones=[{"ticker": "KXMLBGAME-26JUL282140COLSD-COL", "position_fp": "-1.00"}],
            engaged=(True, "rollback_aborted_slippage"),
        )
    assert rc == 1
    clear.assert_not_called()
    inp.assert_not_called()  # ni siquiera pide la confirmación
    assert "abort" in salida.lower()


def test_arrancar_aborta_si_no_pudo_leer_posiciones(settings_del_mes):
    """FAIL-CLOSED: la API caída devuelve None → 'no sé' NO habilita limpiar el freno."""
    with patch("builtins.input", return_value=arranque_mes.CONFIRMACION) as inp:
        rc, salida, clear = _run(
            ["--arrancar"],
            settings=settings_del_mes,
            posiciones=None,
            engaged=(True, "rollback_aborted_slippage"),
        )
    assert rc == 1
    clear.assert_not_called()
    inp.assert_not_called()
    assert "abort" in salida.lower()


def test_arrancar_sin_tty_no_limpia(settings_del_mes):
    """Sin -it en el docker exec, input() lanza EOFError: se aborta en vez de asumir un sí."""
    with patch("builtins.input", side_effect=EOFError):
        rc, salida, clear = _run(
            ["--arrancar"],
            settings=settings_del_mes,
            posiciones=[],
            engaged=(True, "x"),
        )
    assert rc == 1
    clear.assert_not_called()
    assert "tty" in salida.lower()


def test_arrancar_sin_kill_switch_es_noop(settings_del_mes):
    """Sin kill-switch puesto no hay nada que limpiar (y no pide confirmación)."""
    with patch("builtins.input") as inp:
        rc, salida, clear = _run(
            ["--arrancar"], settings=settings_del_mes, posiciones=[], engaged=(False, None)
        )
    assert rc == 0
    clear.assert_not_called()
    inp.assert_not_called()


def test_arrancar_avisa_si_faltan_flags_tras_limpiar(settings_del_mes):
    """Limpiar el switch con flags a medias corre con la config vieja: hay que decirlo."""
    settings_del_mes.MOTOR_9_SPILLOVER_ENABLED = False
    with patch("builtins.input", return_value=arranque_mes.CONFIRMACION):
        rc, salida, clear = _run(
            ["--arrancar"], settings=settings_del_mes, posiciones=[], engaged=(True, "x")
        )
    assert rc == 0
    clear.assert_called_once()
    assert "redeploy" in salida.lower()


# =====================================================
# revisar_flags: la unidad
# =====================================================


def test_revisar_flags_detecta_campo_inexistente():
    """Si un flag del mes no existe en Settings (typo o refactor), se reporta explícito."""
    s = MagicMock(spec=[])  # sin ningún atributo
    faltantes, ok = arranque_mes.revisar_flags(s)
    assert ok == []
    assert len(faltantes) == len(arranque_mes.FLAGS_DEL_MES)
    assert any("no existe en Settings" in f for f in faltantes)


def test_flags_del_mes_existen_en_settings_real():
    """WIRING (lección #155: los flags inertes): cada nombre de FLAGS_DEL_MES debe existir
    de verdad en Settings — si alguien renombra un campo, este test se pone rojo."""
    from src.utils.config import Settings

    campos = set(Settings.model_fields)
    faltan = [n for n in arranque_mes.FLAGS_DEL_MES if n not in campos]
    assert faltan == [], f"flags del mes que NO existen en Settings: {faltan}"


# =====================================================
# Punto ciego del 2026-07-30: la pausa RUNTIME
# =====================================================


def test_estado_pausa_runtime_parsea_status(monkeypatch):
    """El preflight lee is_paused/pause_reason del /status del bot VIVO (el script corre
    por docker exec = proceso aparte; BotState local no sirve). El incidente: 'todo listo'
    con el bot pausado 12 horas por el circuit breaker."""
    import io
    import json

    from scripts.arranque_mes import _estado_pausa_runtime

    payload = json.dumps(
        {"bot": {"is_paused": True, "pause_reason": "circuit_breaker: 3+ rollbacks"}}
    ).encode()

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: _Resp(payload))
    estado = _estado_pausa_runtime()
    assert estado["is_paused"] is True
    assert "circuit_breaker" in estado["pause_reason"]
    assert estado["verificable"] is True


def test_estado_pausa_runtime_no_verificable_es_honesto(monkeypatch):
    """FAIL-SAFE: /status caído → 'no verificable', jamás 'sano' inventado (el veredicto
    lo trata como bloqueante)."""
    import urllib.request

    def _boom(*a, **kw):
        raise OSError("connection refused")

    from scripts.arranque_mes import _estado_pausa_runtime

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    estado = _estado_pausa_runtime()
    assert estado["verificable"] is False
    assert estado["is_paused"] is None


def test_chequeo_bloquea_por_pausa_runtime(settings_del_mes):
    """El incidente 2026-07-30: 'todo listo' con el bot pausado por circuit breaker.
    Ahora la pausa runtime es BLOQUEANTE en el veredicto."""
    rc, salida, _ = _run(
        [],
        settings=settings_del_mes,
        posiciones=[],
        engaged=(False, None),
        pausa={"is_paused": True, "pause_reason": "circuit_breaker: 3+", "verificable": True},
    )
    assert rc == 0
    assert "NO puede arrancar" in salida
    assert "PAUSADO en runtime" in salida
    assert "todo listo" not in salida.lower()


def test_chequeo_pausa_no_verificable_tambien_bloquea(settings_del_mes):
    """FAIL-SAFE: si /status no responde, no se asume sano — bloqueante explícito."""
    rc, salida, _ = _run(
        [],
        settings=settings_del_mes,
        posiciones=[],
        engaged=(False, None),
        pausa={"is_paused": None, "pause_reason": None, "verificable": False},
    )
    assert "NO VERIFICABLE" in salida
    assert "todo listo" not in salida.lower()
