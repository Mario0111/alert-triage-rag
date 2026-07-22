"""Start the triage API automatically when it isn't already running.

The desktop app is a thin client and the API is a heavy server (torch + the
embedding model + Chroma, ~2 GB), so the app can never CONTAIN a backend — it
can only LAUNCH one. This module is that launcher, kept out of the GUI so the
policy is testable and explainable on its own.

Three rules shape it:

1. **Never start what is already running.** A reachable ``/health`` means the
   operator already has a backend (a `triage serve`, a container, a remote
   host); the app attaches to it and — importantly — does NOT stop it on exit.
   Only a backend this app started is a backend this app shuts down.

2. **Only ever autostart a LOCAL backend.** If the API URL points somewhere
   else, starting a server on this machine would not help and would be
   surprising, so the app just reports that the remote API is unreachable.

3. **Use whichever backend the current runtime can actually reach.** Running
   from a source checkout / venv, ``sys.executable`` already has the pipeline
   installed, so ``python -m triage.serve`` is the direct route. Running as the
   FROZEN executable, the bundled interpreter deliberately has none of the
   pipeline (that is why the exe is 47 MB), so the only self-contained backend
   available is the Docker image, whose model and dependencies are baked in.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

# Must match docker-compose.yml. Kept as constants because the frozen app has no
# compose file (and no build context) to read: `docker run` with explicit flags
# is the one form that works from a standalone .exe. If the compose file's
# image tag or volume name changes, change them here too.
DOCKER_IMAGE = "alert-triage-rag:0.4.0"
DOCKER_VOLUME = "alert_triage_rag_chroma-data"
DOCKER_CONTAINER_NAME = "alert-triage-desktop-api"

# Docker Desktop on Windows frequently is not on PATH for GUI-launched
# processes, so fall back to its standard install location before giving up.
_DOCKER_FALLBACK_PATHS = (
    r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
)

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})

# Cold start loads the ~130 MB embedder and opens Chroma; the container also has
# to boot. Generous, because failing early on a working-but-slow start is worse
# than waiting.
_STARTUP_TIMEOUT_S = 180
_POLL_INTERVAL_S = 2


@dataclass
class BackendHandle:
    """A backend THIS app started, and how to stop it again.

    ``None`` is returned instead of a handle when a backend was already running,
    which is what keeps the app from shutting down someone else's server.
    """

    kind: str  # "process" | "docker"
    process: subprocess.Popen[bytes] | None = None

    def stop(self) -> None:
        """Stop the backend we started. Best-effort: never raise on shutdown."""
        try:
            if self.kind == "docker":
                docker = find_docker()
                if docker:
                    subprocess.run(
                        [docker, "stop", DOCKER_CONTAINER_NAME],
                        capture_output=True,
                        timeout=30,
                        check=False,
                    )
            elif self.process is not None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        except OSError:
            # Shutting down is best-effort; a failure here must not stop the
            # app from closing.
            pass


def health_ok(api_url: str, timeout: float = 2.0) -> bool:
    """True when ``GET {api_url}/health`` answers 2xx.

    Reaching /health at all means the server's startup completed, which includes
    the store/fingerprint check — so "healthy" really does mean "ready".
    """
    try:
        with urllib.request.urlopen(f"{api_url}/health", timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError):
        return False


def is_local(api_url: str) -> bool:
    """True when the URL points at this machine (the only thing we may start)."""
    return (urlparse(api_url).hostname or "") in _LOCAL_HOSTS


def find_docker() -> str | None:
    """Locate the docker executable on PATH, else at its usual Windows path."""
    found = shutil.which("docker")
    if found:
        return found
    return next((p for p in _DOCKER_FALLBACK_PATHS if os.path.isfile(p)), None)


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle (see packaging/*.spec)."""
    return getattr(sys, "frozen", False)


def _port_of(api_url: str) -> int:
    return urlparse(api_url).port or 8000


# Keep spawned children from flashing a console window on Windows. The GUI app
# is built with console=False, so a subprocess would otherwise pop up its own
# black window. The flag does not exist off Windows, where 0 means "no flags".
_CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _start_docker(api_url: str) -> BackendHandle:
    """Run the API image as a detached container (the frozen-app route).

    Mirrors docker-compose.yml: the same named volume (so the ingested store is
    reused), the API key passed by NAME only so it never lands in an image or a
    file, and the host side of the port pinned to loopback.
    """
    docker = find_docker()
    if docker is None:
        raise RuntimeError(
            "Docker was not found. The packaged app starts its backend in a "
            "container, so Docker Desktop must be installed and running."
        )
    port = _port_of(api_url)
    # --rm so a stopped container leaves nothing to clean up by hand.
    command = [
        docker, "run", "--rm", "-d",
        "--name", DOCKER_CONTAINER_NAME,
        "-p", f"127.0.0.1:{port}:8000",
        "-e", "ANTHROPIC_API_KEY",
        "-v", f"{DOCKER_VOLUME}:/data",
        DOCKER_IMAGE,
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        creationflags=_CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        if "is already in use" in message:
            # A previous run left it behind; adopt it rather than failing.
            return BackendHandle(kind="docker")
        # By far the most common failure is "Docker is installed but not
        # started" — say that in plain words instead of forwarding the raw
        # named-pipe/socket error the user can do nothing with.
        if "daemon is running" in message or "cannot find the file" in message.lower():
            raise RuntimeError(
                "Docker does not appear to be running. Start Docker Desktop, "
                "wait for it to finish starting, then try again."
            )
        raise RuntimeError(f"Could not start the backend container: {message}")
    return BackendHandle(kind="docker")


def _start_local_server(api_url: str) -> BackendHandle:
    """Spawn ``python -m triage.serve`` (the from-source route).

    Uses ``sys.executable`` deliberately: the interpreter running this code is
    the one whose environment has the pipeline installed.
    """
    port = _port_of(api_url)
    process = subprocess.Popen(
        [sys.executable, "-m", "triage.serve", "--host", "127.0.0.1",
         "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW,
    )
    return BackendHandle(kind="process", process=process)


def ensure_backend(
    api_url: str,
    on_status: Callable[[str], None] | None = None,
    timeout_s: int = _STARTUP_TIMEOUT_S,
) -> BackendHandle | None:
    """Make sure the API at ``api_url`` is reachable, starting it if needed.

    Args:
        api_url: Base URL of the triage API (no trailing slash).
        on_status: Optional progress callback for the UI (called with short
            human-readable lines).
        timeout_s: How long to wait for a started backend to become healthy.

    Returns:
        A `BackendHandle` if THIS call started a backend (the caller owns
        stopping it), or ``None`` if one was already running or the URL is
        remote — in which case the caller must not stop anything.

    Raises:
        RuntimeError: If a backend could not be started, or did not become
            healthy before ``timeout_s``.
    """
    report = on_status or (lambda _message: None)

    report("Checking for a running API…")
    if health_ok(api_url):
        report("Connected to the running API.")
        return None

    if not is_local(api_url):
        raise RuntimeError(
            f"No API is answering at {api_url}, and it is not a local address, "
            "so it cannot be started from here."
        )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set in this environment. The triage API "
            "reads the key from the environment and will refuse to start "
            "without it."
        )

    if is_frozen():
        report("Starting the backend container (first run can take a minute)…")
        handle = _start_docker(api_url)
    else:
        report("Starting a local triage server…")
        handle = _start_local_server(api_url)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if health_ok(api_url):
            report("Backend ready.")
            return handle
        # A local server that died (bad store, missing key) will never become
        # healthy — surface that immediately instead of waiting out the clock.
        if handle.process is not None and handle.process.poll() is not None:
            raise RuntimeError(
                "The triage server exited during startup. The usual cause is a "
                "missing or stale store — run `triage ingest`."
            )
        time.sleep(_POLL_INTERVAL_S)

    handle.stop()
    raise RuntimeError(
        f"The backend did not become healthy within {timeout_s}s. If this is a "
        "first run, the store may still need building (`triage ingest`, or "
        "`docker compose run --rm api ingest`)."
    )
