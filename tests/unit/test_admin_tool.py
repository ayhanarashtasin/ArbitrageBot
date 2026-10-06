import json

import httpx
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.tools.admin import main, setup
from tests.unit.test_runtime import runtime


def test_setup_generates_a_private_token_and_preserves_existing_configuration(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = tmp_path / ".env"
    env.write_text("RUN_MODE=paper\nZERO_DELTA_DATABASE_URL='private-url'\n", encoding="utf-8")
    assert main(["setup"]) == 0
    values = dotenv_values(env)
    token = values["ADMIN_API_TOKEN"]
    assert len(token) >= 43 and values["ZERO_DELTA_DATABASE_URL"] == "private-url"
    assert token not in capsys.readouterr().out
    assert not setup(env)["created"]
    assert dotenv_values(env)["ADMIN_API_TOKEN"] == token


def test_check_authenticates_against_the_real_api_validation_without_changing_the_kill_switch(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_API_TOKEN", "private-admin-token")
    rt = runtime(admin_api_token="private-admin-token")
    api = TestClient(create_app(rt, start_runner=False))

    def request(req):
        assert json.loads(req.content) == {}
        result = api.post(req.url.path, headers={"X-Admin-Token": req.headers["X-Admin-Token"]}, json={})
        return httpx.Response(result.status_code, content=result.content)

    assert main(["check"], transport=httpx.MockTransport(request)) == 0
    assert not rt.kill.pause_new_entries and not rt.kill.emergency_flatten
    assert "private-admin-token" not in capsys.readouterr().out


def test_check_reports_rejected_authentication_without_exposing_the_token(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_API_TOKEN", "private-admin-token")
    assert main(["check"], transport=httpx.MockTransport(lambda r: httpx.Response(401))) == 1
    out = json.loads(capsys.readouterr().out)
    assert not out["authenticated"] and out["http_status"] == 401
