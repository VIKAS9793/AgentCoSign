"""The operator command line must work when the web app cannot.

Edge cases, written before the tests:

| Case | Expected |
|---|---|
| Loading the command line | the web app module is never imported |
| Stop refunds while the web app cannot load | refunds stop and the stop is recorded |
| Stop refunds on a data folder not yet created | folder created, stop recorded |
| Stop refunds twice | second run says already stopped, records nothing new |
| Revoke a phone on a data folder not yet created | no crash, nothing to revoke |
| Open passkeys before the ledger on a new folder | opens, no passkeys |
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentcosign import cli, wiring
from agentcosign.core.config import Settings, get_settings

REPO = Path(__file__).resolve().parents[2]
AUDIT_KEY = "k" * 48


def _settings(monkeypatch: pytest.MonkeyPatch, database: Path) -> Settings:
    """Settings the way an operator's shell provides them."""
    monkeypatch.setenv("AGENTCOSIGN_AUDIT_KEY", AUDIT_KEY)
    monkeypatch.setenv("SQLITE_DB_PATH", str(database))
    get_settings.cache_clear()
    try:
        return get_settings()
    finally:
        get_settings.cache_clear()  # later tests load their own settings


def _run(code: str, tmp_path: Path, database: Path) -> subprocess.CompletedProcess[str]:
    """Run Python in a clean process, outside the checkout, with operator settings."""
    env = {
        **os.environ,
        "AGENTCOSIGN_CONFIG": str(REPO / "config.yaml"),
        "AGENTCOSIGN_AUDIT_KEY": AUDIT_KEY,
        "SQLITE_DB_PATH": str(database),
        "PYTHONPATH": str(REPO),
    }
    return subprocess.run(  # noqa: S603 -- fixed interpreter and test-owned code
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.edge
def test_the_command_line_never_loads_the_web_app(tmp_path: Path) -> None:
    code = (
        "import sys\n"
        "import agentcosign.cli\n"
        "loaded = [m for m in sys.modules if m == 'agentcosign.main'"
        " or m.startswith('agentcosign.api')]\n"
        "print(loaded)\n"
    )
    result = _run(code, tmp_path, tmp_path / "unused.db")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


@pytest.mark.edge
def test_refunds_stop_even_when_the_web_app_cannot_load(tmp_path: Path) -> None:
    database = tmp_path / "fresh" / "data" / "audit.db"
    code = (
        "import sys\n"
        "sys.modules['agentcosign.main'] = None  # importing the web app now fails\n"
        "from agentcosign import cli\n"
        "sys.exit(cli.main(['stop-refunds']))\n"
    )
    result = _run(code, tmp_path, database)
    assert result.returncode == 0, result.stderr
    assert "All refunds are stopped." in result.stdout
    assert database.is_file()


@pytest.mark.edge
def test_stopping_on_a_new_data_folder_records_the_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch, tmp_path / "new" / "audit.db")
    assert cli.stop_refunds(settings) == 0
    ledger = wiring.open_ledger(settings)
    assert ledger.refunds_stopped()
    assert ledger.verify_chain() >= 1


@pytest.mark.edge
def test_stopping_twice_says_so_and_records_nothing_new(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(monkeypatch, tmp_path / "audit.db")
    assert cli.stop_refunds(settings) == 0
    entries = wiring.open_ledger(settings).verify_chain()
    assert cli.stop_refunds(settings) == 0
    assert "Refunds were already stopped." in capsys.readouterr().out
    assert wiring.open_ledger(settings).verify_chain() == entries


@pytest.mark.edge
def test_revoking_on_a_new_data_folder_finds_nothing_and_does_not_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(monkeypatch, tmp_path / "missing" / "audit.db")
    assert cli.revoke_approver(settings, "Lost") == 1
    assert "No active passkey for Lost. Nothing changed." in capsys.readouterr().out


@pytest.mark.edge
def test_passkeys_open_on_a_new_data_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch, tmp_path / "other" / "audit.db")
    assert not wiring.open_passkeys(settings).has_passkeys()
