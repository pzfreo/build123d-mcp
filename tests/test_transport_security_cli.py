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

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    # Keep main() from touching process-wide server state.
    monkeypatch.setattr(server, "configure", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "apply_tool_visibility", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "configure_registry", lambda *_a, **_k: None)
    monkeypatch.setattr(server.mcp, "run", lambda: None)
    for k in ("BUILD123D_ALLOW_ALL", "BUILD123D_ALLOWED_HOSTS", "BUILD123D_ALLOWED_ORIGINS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("sys.argv", ["build123d-mcp", "--in-process", *argv])
    cli.main()


def test_conflict_is_an_error_for_http(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _run_main(
            monkeypatch,
            ["--transport", "http", "--allow-all", "--allowed-hosts", "a.example.com"],
        )
    err = capsys.readouterr().err
    assert "incompatible" in err and "--no-allow-all" in err


def test_conflict_ignored_for_stdio(monkeypatch):
    _run_main(monkeypatch, ["--allowed-hosts", "a.example.com"], {"BUILD123D_ALLOW_ALL": "1"})
    assert server._transport_security is None


def test_cli_clears_env_allowlist_for_allow_all(monkeypatch):
    _run_main(
        monkeypatch,
        ["--transport", "http", "--allow-all", "--allowed-hosts", ""],
        {"BUILD123D_ALLOWED_HOSTS": "a.example.com"},
    )
    assert server._transport_security.enable_dns_rebinding_protection is False


def test_cli_overrides_env_allow_all(monkeypatch):
    _run_main(
        monkeypatch,
        ["--transport", "http", "--no-allow-all", "--allowed-hosts", "a.example.com"],
        {"BUILD123D_ALLOW_ALL": "1"},
    )
    assert server._transport_security.enable_dns_rebinding_protection is True
    assert "a.example.com" in server._transport_security.allowed_hosts


@pytest.mark.parametrize(
    "argv",
    [
        ["--allowed-hosts", "*"],
        ["--allowed-hosts", "*.example.com"],
        ["--allowed-hosts", "*:*"],
        ["--allowed-hosts", "::1"],
        ["--allowed-origins", "cad.example.com"],
        ["--allowed-origins", "https://*.example.com"],
    ],
)
def test_entries_that_can_never_match_are_rejected(monkeypatch, argv):
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, ["--transport", "http", *argv])


@pytest.mark.parametrize(
    "argv",
    [
        ["--allowed-hosts", "cad.example.com,cad.example.com:*,[::1]:*"],
        ["--allowed-origins", "https://cad.example.com,http://10.0.0.1:*"],
    ],
)
def test_valid_entries_are_accepted(monkeypatch, argv):
    _run_main(monkeypatch, ["--transport", "http", *argv])
    assert server._transport_security is not None


def _status(settings, host, origin=None):
    """Status the SDK's middleware would return for a POST with these headers."""
    import asyncio

    from mcp.server.transport_security import TransportSecurityMiddleware
    from starlette.requests import Request

    headers = [(b"host", host.encode()), (b"content-type", b"application/json")]
    if origin:
        headers.append((b"origin", origin.encode()))
    request = Request({"type": "http", "method": "POST", "headers": headers})
    mw = TransportSecurityMiddleware(settings)
    response = asyncio.run(mw.validate_request(request, is_post=True))
    return 200 if response is None else response.status_code


def test_requests_with_additive_allowlists():
    s = build(hosts="cad.example.com,cad.example.com:*", origins="https://cad.example.com")
    assert _status(s, "cad.example.com") == 200
    assert _status(s, "cad.example.com:8000") == 200
    assert _status(s, "127.0.0.1:8000") == 200
    assert _status(s, "localhost:8000") == 200
    assert _status(s, "[::1]:8000") == 200
    assert _status(s, "evil.com") == 421
    assert _status(s, "cad.example.com", "https://cad.example.com") == 200
    assert _status(s, "cad.example.com", "http://localhost:3000") == 200
    assert _status(s, "cad.example.com", "http://[::1]:3000") == 200
    assert _status(s, "cad.example.com", "http://evil.com") == 403


def test_origins_alone_still_accept_localhost_requests():
    s = build(origins="https://cad.example.com")
    assert _status(s, "127.0.0.1:8000", "http://localhost:3000") == 200
    assert _status(s, "evil.com") == 421


def test_allow_all_accepts_any_host_and_origin():
    s = build(allow_all=True)
    assert _status(s, "evil.com", "http://evil.com") == 200


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
