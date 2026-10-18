"""Configuration loading (no network)."""
import shutil
from pathlib import Path

from northstar.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_env_example_loads_with_blank_values(tmp_path, monkeypatch):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env")
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.discord_channel_id is None and s.helius_api_key is None
    assert s.dry_run is True and s.min_opportunity_score == 88
    assert sum(s.weights.values()) == 100


def test_helius_url_never_in_label():
    s = Settings(_env_file=None, helius_api_key="abc123secretkey")
    assert "abc123secretkey" in s.effective_rpc_url
    assert "abc123secretkey" not in s.rpc_label
    assert "abc123secretkey" in s.secrets()


def test_config_problems_flag_impossible_narrative_threshold_and_weights():
    s = Settings(_env_file=None, min_narrative_score=40, w_narrative=20, w_evidence=15, w_demand=20,
                 w_liquidity=20, w_holders=15, w_momentum=10, w_wallet_quality=5)
    problems = " | ".join(s.config_problems())
    assert "MIN_NARRATIVE_SCORE=40" in problems and "32.0 narrative points" in problems
    assert "sum to 105" in problems


def test_default_config_has_no_problems():
    assert Settings(_env_file=None).config_problems() == []


def test_trailing_spaces_in_choice_settings_are_tolerated():
    s = Settings(_env_file=None, min_confidence="MEDIUM ", max_allowed_risk=" moderate")
    assert s.min_confidence == "MEDIUM" and s.max_allowed_risk == "MODERATE"
