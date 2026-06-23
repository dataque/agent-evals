"""The dependency-free .env loader + ${VAR} expansion."""

from __future__ import annotations

import os

from agent_evals.envfile import expand_env, load_dotenv


def test_load_dotenv_parses_and_does_not_override(tmp_path, monkeypatch):
    monkeypatch.delenv("DOTENV_A", raising=False)
    monkeypatch.setenv("DOTENV_EXISTING", "preset")
    env = tmp_path / ".env"
    env.write_text('# comment\nexport DOTENV_A=00012345\nDOTENV_Q="hi there"\nDOTENV_EXISTING=fromfile\n')

    loaded = load_dotenv(env)
    try:
        assert os.environ["DOTENV_A"] == "00012345"      # `export ` stripped
        assert os.environ["DOTENV_Q"] == "hi there"       # quotes stripped
        assert os.environ["DOTENV_EXISTING"] == "preset"  # pre-existing env not overridden
        assert loaded["DOTENV_EXISTING"] == "fromfile"
    finally:  # load_dotenv writes os.environ directly; monkeypatch won't revert these
        for k in ("DOTENV_A", "DOTENV_Q"):
            os.environ.pop(k, None)


def test_load_dotenv_missing_file_is_noop(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == {}


def test_expand_env(monkeypatch):
    monkeypatch.setenv("AGENT_EVALS_USER_LOGIN_ID", "00012345")
    monkeypatch.delenv("MISSING_VAR", raising=False)
    cfg = {
        "auth": {"user_login_id": "${AGENT_EVALS_USER_LOGIN_ID}"},
        "url": "https://h/${MISSING_VAR:-api}/x",   # default used when unset
        "timeout": 120,                              # non-strings untouched
        "list": ["${AGENT_EVALS_USER_LOGIN_ID}"],
    }
    out = expand_env(cfg)
    assert out["auth"]["user_login_id"] == "00012345"
    assert out["url"] == "https://h/api/x"
    assert out["timeout"] == 120
    assert out["list"] == ["00012345"]
