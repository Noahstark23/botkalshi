"""El CLI no filtra secretos al fallar y el texto no certifica rentabilidad."""

from unittest.mock import AsyncMock, patch

from scripts.check_portfolio import main, render_text


def test_cli_failure_redacts_secret_even_when_exception_contains_it(capsys):
    with (
        patch("sys.argv", ["check_portfolio", "--json"]),
        patch(
            "scripts.check_portfolio.run",
            new_callable=AsyncMock,
            side_effect=RuntimeError("secret-key-and-private-material"),
        ),
    ):
        assert main() == 1
    out = capsys.readouterr().out
    assert "secret-key-and-private-material" not in out
    assert '"authorizes_trading": false' in out
    assert "ATENCION" in out


def test_text_unknown_data_is_not_presented_as_zero():
    out = render_text({"status": "ATENCION", "errors": ["faltan datos"]})
    assert "DESCONOCIDO" in out
    assert "NO EVALUADO" in out
    assert "no autoriza trading" in out
