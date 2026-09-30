from pathlib import Path

from pytubefix import YouTube
from pytubefix.sabr.browser_session import browser_profile_dir, browser_mode


def test_login_gated_player_response_uses_browser_session():
    assert YouTube._needs_browser_player({
        "playabilityStatus": {
            "status": "LOGIN_REQUIRED",
            "reason": "Sign in to confirm your age",
        }
    })
    assert YouTube._needs_browser_player({
        "playabilityStatus": {
            "status": "AGE_CHECK_REQUIRED",
        }
    })


def test_non_login_gated_player_response_does_not_use_browser_session():
    assert not YouTube._needs_browser_player({
        "playabilityStatus": {
            "status": "ERROR",
            "reason": "Video unavailable",
        }
    })
    assert not YouTube._needs_browser_player({
        "playabilityStatus": {
            "status": "OK",
        }
    })


def test_browser_profile_dir_expands_user_and_environment(monkeypatch):
    monkeypatch.setenv("PYTUBEFIX_SABR_BROWSER_PROFILE_DIR", "relative-profile")
    resolved = browser_profile_dir()
    assert resolved == str((Path.cwd() / "relative-profile").resolve())


def test_browser_mode_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("PYTUBEFIX_SABR_BROWSER_MODE", "unknown")
    try:
        browser_mode()
    except Exception as exc:
        assert "must be auto" in str(exc)
    else:
        raise AssertionError("unknown browser mode should fail")
