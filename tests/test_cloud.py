"""Credentials: where the stdio server's sign-in comes from, and how it stays fresh."""
import json
import time

import pytest

from phone_harness_mcp.cloud import CliLogin, Cloud, CloudError, NotSignedIn, StaticToken, local_credentials


def write_auth(home, **record):
    path = home / "config" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record))
    return path


def test_env_key_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("PHONE_HARNESS_HOME", str(tmp_path))
    write_auth(tmp_path, access_token="from_cli", expires_at=time.time() + 3600)
    monkeypatch.setenv("PHONE_HARNESS_API_KEY", " pck_env ")
    creds = local_credentials()
    assert isinstance(creds, StaticToken) and creds.token() == "pck_env"
    assert local_credentials("pck_flag").token() == "pck_flag"


def test_cli_login_is_read_from_the_cli_config_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("PHONE_HARNESS_API_KEY", raising=False)
    monkeypatch.setenv("PHONE_HARNESS_HOME", str(tmp_path))
    write_auth(tmp_path, access_token="from_cli", refresh_token="rt", expires_at=time.time() + 3600)
    creds = local_credentials()
    assert isinstance(creds, CliLogin) and creds.token() == "from_cli"


def test_not_signed_in_says_what_to_do(monkeypatch, tmp_path):
    monkeypatch.delenv("PHONE_HARNESS_API_KEY", raising=False)
    monkeypatch.setenv("PHONE_HARNESS_HOME", str(tmp_path))
    with pytest.raises(NotSignedIn) as e:
        local_credentials().token()
    assert "phone-harness cloud login" in str(e.value) and "PHONE_HARNESS_API_KEY" in str(e.value)


def test_expired_login_is_refreshed_and_saved(api, tmp_path):
    path = write_auth(tmp_path, access_token="at_expired", refresh_token="rt_old", expires_at=time.time() - 10,
                      email="me@example.com")
    creds = CliLogin(config_dir=tmp_path / "config", issuer=api.url, client_id="cli")
    assert creds.token() == "at_new"
    saved = json.loads(path.read_text())
    assert saved["access_token"] == "at_new" and saved["refresh_token"] == "rt_new"
    assert saved["expires_at"] > time.time() + 3000 and saved["email"] == "me@example.com"
    assert ("POST", "/oauth/token", None) in api.requests


def test_a_refused_token_is_refreshed_once_then_retried(api, tmp_path):
    # Still "valid" by its own clock, but the API no longer takes it.
    write_auth(tmp_path, access_token="at_revoked", refresh_token="rt_old", expires_at=time.time() + 3600)
    cloud = Cloud(CliLogin(config_dir=tmp_path / "config", issuer=api.url, client_id="cli"), api.url)
    me = cloud.request("GET", "/me")
    assert me["email"] == "me@example.com"
    bearers = [b for m, p, b in api.requests if p == "/me"]
    assert bearers == ["at_revoked", "at_new"]


def test_a_refused_key_is_a_401_not_a_loop(api):
    cloud = Cloud(StaticToken("pck_wrong"), api.url)
    with pytest.raises(CloudError) as e:
        cloud.request("GET", "/me")
    assert e.value.status == 401
    assert len([p for p in api.requests if p[1] == "/me"]) == 1
