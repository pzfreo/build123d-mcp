"""CLI flags -> TransportSecuritySettings -> http_app() (PR #492)."""

import pytest

from build123d_mcp import cli, server


@pytest.fixture(autouse=True)
def _reset_transport_security():
    server.configure_transport_security(None)
    yield
    server.configure_transport_security(None)


def build(allow_all=False, hosts="", origins=""):
    return cli._build_transport_security_settings(allow_all, cli._csv(hosts), cli._csv(origins))


def test_no_flags_leaves_sdk_default():
    assert build() is None


@pytest.mark.parametrize("value", ["", " ", ",", " , ,"])
def test_blank_lists_count_as_unset(value):
    assert cli._csv(value) == []
    assert build(hosts=value, origins=value) is None


def test_hosts_add_to_localhost_defaults():
    s = build(hosts="cad.example.com:*")
    assert s.enable_dns_rebinding_protection
    assert "cad.example.com:*" in s.allowed_hosts
    assert "127.0.0.1:*" in s.allowed_hosts
    assert "http://localhost:*" in s.allowed_origins  # origins not emptied


def test_origins_alone_keep_localhost_hosts():
    s = build(origins="https://cad.example.com")
    assert "localhost:*" in s.allowed_hosts
    assert "https://cad.example.com" in s.allowed_origins


def test_allow_all_disables_protection():
    assert build(allow_all=True).enable_dns_rebinding_protection is False


def _run_main(monkeypatch, argv, env=None):
    import uvicorn

    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app))
    for k in ("BUILD123D_ALLOW_ALL", "BUILD123D_ALLOWED_HOSTS", "BUILD123D_ALLOWED_ORIGINS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("sys.argv", ["build123d-mcp", "--in-process", *argv])
    prev = getattr(server, "_session", None)
    try:
        cli.main()
    finally:
        if prev is not None:
            server._session = prev
    return captured


def test_conflict_is_an_error_for_http(monkeypatch):
    with pytest.raises(SystemExit):
        _run_main(
            monkeypatch,
            ["--transport", "http", "--allow-all", "--allowed-hosts", "a.example.com"],
        )


def test_conflict_ignored_for_stdio(monkeypatch):
    monkeypatch.setattr(server.mcp, "run", lambda: None)
    _run_main(monkeypatch, ["--allowed-hosts", "a.example.com"], {"BUILD123D_ALLOW_ALL": "1"})


def test_cli_overrides_env_allow_all(monkeypatch):
    _run_main(
        monkeypatch,
        ["--transport", "http", "--no-allow-all", "--allowed-hosts", "a.example.com"],
        {"BUILD123D_ALLOW_ALL": "1"},
    )
    assert server._transport_security.enable_dns_rebinding_protection is True
    assert "a.example.com" in server._transport_security.allowed_hosts


def test_setting_reaches_streamable_http_app(monkeypatch):
    seen = {}
    real = server.mcp.streamable_http_app

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(server.mcp, "streamable_http_app", spy)
    settings = build(hosts="cad.example.com")
    server.configure_transport_security(settings)
    server.http_app()
    assert seen["transport_security"] is settings
