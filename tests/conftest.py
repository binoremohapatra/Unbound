"""Every test gets its own OMNICONTROL_HOME, so no test can touch a real %APPDATA% / ~/.config."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNICONTROL_HOME", str(tmp_path / "omni_home"))
