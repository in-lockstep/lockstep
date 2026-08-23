"""Discovery records a surface the pipeline declared. It never supplies one of its own."""

from __future__ import annotations

import json

import httpx
import pytest
from click.testing import CliRunner
from pipeline_exec import cli
from pipeline_exec.builtins import discovery
from pipeline_exec.builtins.discovery import Surface, discover_api, write_context
from pipeline_exec.config import ExecConfig
from pipeline_exec.errors import ExecError

OPENAPI = {
    "info": {"title": "Petstore"},
    "paths": {"/pets": {"get": {}, "post": {}}, "/owners": {"get": {}}},
}


def routes(monkeypatch, handler):
    """Point the module's client at a mock transport, keeping the real client's behaviour."""
    real = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("verify", None)
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(discovery.httpx, "AsyncClient", factory)


def config(**overrides):
    return ExecConfig(profile_api_url="https://api.example.test", **overrides)


def surface_file(tmp_path, body):
    path = tmp_path / "api-surface.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# --- the declared surface --------------------------------------------------


def test_a_surface_must_declare_something(tmp_path):
    with pytest.raises(ExecError) as excinfo:
        Surface.load(surface_file(tmp_path, "paths: []\n"))
    assert "neither" in str(excinfo.value)


def test_a_missing_surface_is_an_error_not_an_empty_result(tmp_path):
    with pytest.raises(ExecError):
        Surface.load(tmp_path / "nothing.yaml")


def test_a_surface_declares_paths_or_a_document(tmp_path):
    loaded = Surface.load(surface_file(tmp_path, "openapi: /openapi.json\npaths: [/pets]\n"))
    assert loaded.openapi == "/openapi.json"
    assert loaded.paths == ["/pets"]


# --- what it reads ---------------------------------------------------------


def test_a_published_document_is_taken_whole(monkeypatch):
    routes(monkeypatch, lambda request: httpx.Response(200, json=OPENAPI))
    result = _run(config(), Surface(openapi="/openapi.json"))
    assert result["openapi"]["paths"].keys() == {"/pets", "/owners"}


def test_declared_paths_are_probed_and_recorded(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"items": []}, headers={"content-type": "application/json"})

    routes(monkeypatch, handler)
    result = _run(config(), Surface(paths=["/pets", "/owners"]))
    assert seen == ["/pets", "/owners"]
    assert result["paths"]["/pets"]["sample"] == {"items": []}


def test_probing_only_reads(monkeypatch):
    """The version this replaced POSTed invented payloads at the target to learn its schema."""
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(200, json={})

    routes(monkeypatch, handler)
    _run(config(), Surface(paths=["/pets", "/owners"]))
    assert set(methods) == {"GET"}


def test_an_endpoint_that_is_not_there_is_recorded_not_hidden(monkeypatch):
    routes(monkeypatch, lambda request: httpx.Response(404))
    result = _run(config(), Surface(paths=["/gone"]))
    assert result["paths"]["/gone"]["status"] == 404


def test_a_target_that_refuses_the_connection_does_not_fail_the_rest(monkeypatch):
    def handler(request):
        if request.url.path == "/down":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={})

    routes(monkeypatch, handler)
    result = _run(config(), Surface(paths=["/down", "/up"]))
    assert "/down" not in result["paths"]
    assert result["paths"]["/up"]["status"] == 200


def test_discovery_needs_to_know_what_to_look_at():
    with pytest.raises(ExecError) as excinfo:
        _run(ExecConfig(), Surface(paths=["/pets"]))
    assert "api_url" in str(excinfo.value)


def test_an_auth_method_it_cannot_perform_says_so_rather_than_probing_anonymously():
    with pytest.raises(ExecError) as excinfo:
        _run(config(profile_auth_method="jwt"), Surface(paths=["/pets"]))
    assert "--token" in str(excinfo.value)


def test_basic_auth_is_sent(monkeypatch):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={})

    routes(monkeypatch, handler)
    settings = config(profile_auth_method="basic", profile_username="u", profile_password="p")
    _run(settings, Surface(paths=["/pets"]))
    assert seen["auth"].startswith("Basic ")


# --- what it writes --------------------------------------------------------


def test_what_was_found_is_written_as_a_context(tmp_path):
    path = tmp_path / "contexts" / "discovered-api.md"
    write_context({"api_url": "https://api.example.test", "openapi": OPENAPI}, path)
    text = path.read_text()
    assert text.startswith("---\nname: discovered-api\n")
    assert "`/pets` — GET, POST" in text
    assert "Petstore" in text


def test_a_probed_surface_is_written_as_a_context(tmp_path):
    path = tmp_path / "discovered-api.md"
    write_context({"api_url": "https://api.example.test", "paths": {"/pets": {"status": 200}}}, path)
    assert "`/pets` — HTTP 200" in path.read_text()


# --- the command -----------------------------------------------------------


def test_the_command_writes_both_the_json_and_the_context(monkeypatch, tmp_path):
    routes(monkeypatch, lambda request: httpx.Response(200, json=OPENAPI))
    monkeypatch.setenv("PROFILE_API_URL", "https://api.example.test")
    monkeypatch.setenv("PROFILE_AUTH_METHOD", "none")
    surface = surface_file(tmp_path, "openapi: /openapi.json\n")
    out = tmp_path / "surface.json"
    context = tmp_path / "discovered-api.md"

    result = CliRunner().invoke(
        cli.main,
        ["discover", f"--surface={surface}", f"--output={out}", f"--context={context}"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["openapi"]["info"]["title"] == "Petstore"
    assert "discovered-api" in context.read_text()


def test_the_command_refuses_to_guess_a_surface(tmp_path):
    result = CliRunner().invoke(cli.main, ["discover", f"--output={tmp_path / 'o.json'}"])
    assert result.exit_code != 0
    assert "--surface" in result.output


def _run(config, surface, **kwargs):
    import asyncio

    return asyncio.run(discover_api(config, surface, **kwargs))
