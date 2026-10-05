"""El exportador publica ATENCIÓN al fallar; no conserva un OK indefinidamente."""

from unittest.mock import AsyncMock, patch

from scripts.export_operator_report import main
from src.monitoring.operator_report_files import read_report


def test_configuration_failure_publishes_unknown_without_secret(tmp_path, capsys):
    path = tmp_path / "report.json"
    with (
        patch("sys.argv", ["export", "--output", str(path)]),
        patch(
            "scripts.export_operator_report.run",
            new_callable=AsyncMock,
            side_effect=RuntimeError("PRIVATE_SECRET"),
        ),
    ):
        assert main() == 1
    assert "PRIVATE_SECRET" not in path.read_text()
    report = read_report(path)
    assert report["status"] == "ATENCION"
    assert "balance" not in report
    assert capsys.readouterr().out == ""


def test_publication_failure_is_explicit_without_private_details(tmp_path, capsys):
    with (
        patch("sys.argv", ["export", "--output", str(tmp_path / "report")]),
        patch(
            "scripts.export_operator_report.run",
            new_callable=AsyncMock,
            return_value={"status": "OK"},
        ),
        patch("scripts.export_operator_report.publish_report", side_effect=OSError("SECRET_PATH")),
    ):
        assert main() == 1
    out = capsys.readouterr().out
    assert "ATENCION" in out
    assert "SECRET_PATH" not in out
