"""Backend autostart policy (triage/backend.py).

Hermetic by construction: every test that could otherwise spawn a process or a
container patches the one function that would do it, so the SUITE never starts a
server, never talks to Docker, and never touches the network. What is actually
under test is the POLICY — when to start something, when to attach to what is
already there, and what the caller is then allowed to stop.
"""

from __future__ import annotations

from typing import Any

import pytest

from triage import backend


def test_is_local_recognises_loopback_and_rejects_remote() -> None:
    assert backend.is_local("http://127.0.0.1:8000")
    assert backend.is_local("http://localhost:8000")
    assert not backend.is_local("http://api.example.com:8000")
    # A compose service name is another machine as far as this app is concerned.
    assert not backend.is_local("http://api:8000")


def test_health_ok_is_false_when_nothing_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_: Any, **__: Any) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(backend.urllib.request, "urlopen", boom)
    assert backend.health_ok("http://127.0.0.1:8000") is False


def test_already_running_backend_is_adopted_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "health_ok", lambda *_a, **_k: True)
    # If either starter were called the test would fail loudly.
    monkeypatch.setattr(
        backend, "_start_docker", lambda *_a: pytest.fail("must not start docker")
    )
    monkeypatch.setattr(
        backend, "_start_local_server", lambda *_a: pytest.fail("must not spawn")
    )

    handle = backend.ensure_backend("http://127.0.0.1:8000")

    # None is the contract for "already running": the caller did not start it,
    # so the caller must not stop it.
    assert handle is None


def test_remote_url_is_never_started_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend, "health_ok", lambda *_a, **_k: False)
    monkeypatch.setattr(
        backend, "_start_docker", lambda *_a: pytest.fail("must not start docker")
    )

    with pytest.raises(RuntimeError, match="not a local address"):
        backend.ensure_backend("http://api.example.com:8000")


def test_missing_api_key_fails_before_starting_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(backend, "health_ok", lambda *_a, **_k: False)
    monkeypatch.setattr(
        backend, "_start_docker", lambda *_a: pytest.fail("must not start docker")
    )

    # Cheapest check first: the container would only fail its own startup.
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        backend.ensure_backend("http://127.0.0.1:8000")


def test_started_backend_is_returned_once_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # Unhealthy on the first probe, healthy on the next: the start-then-wait path.
    probes = iter([False, True])
    monkeypatch.setattr(
        backend, "health_ok", lambda *_a, **_k: next(probes, True)
    )
    monkeypatch.setattr(backend, "is_frozen", lambda: False)
    started = backend.BackendHandle(kind="process")
    monkeypatch.setattr(backend, "_start_local_server", lambda *_a: started)
    monkeypatch.setattr(backend.time, "sleep", lambda _s: None)

    statuses: list[str] = []
    handle = backend.ensure_backend("http://127.0.0.1:8000", statuses.append)

    # A handle means WE started it, so the caller owns shutting it down.
    assert handle is started
    assert any("Starting" in s for s in statuses)


def test_frozen_app_uses_docker_not_a_local_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    probes = iter([False, True])
    monkeypatch.setattr(backend, "health_ok", lambda *_a, **_k: next(probes, True))
    monkeypatch.setattr(backend, "is_frozen", lambda: True)
    monkeypatch.setattr(backend.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        backend,
        "_start_local_server",
        lambda *_a: pytest.fail("frozen app has no pipeline to serve"),
    )
    docker_handle = backend.BackendHandle(kind="docker")
    monkeypatch.setattr(backend, "_start_docker", lambda *_a: docker_handle)

    assert backend.ensure_backend("http://127.0.0.1:8000") is docker_handle
