"""Permission-gated web search, page fetch, and evidence helpers."""
from __future__ import annotations

import os
import ipaddress
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx
import yaml

from shamsu import __version__

from shamsu.safety.approval import ask_approval
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.session.manager import SessionLogger
from shamsu.types import ApprovalRequest
from shamsu import paths

_TRAFILATURA_NOT_LOADED = object()
trafilatura: Any = _TRAFILATURA_NOT_LOADED

DEFAULT_USER_AGENT = f"SHAMSU/{__version__} (+local coding agent)"
DEFAULT_SEARXNG_URL = "http://localhost:8095"
DEFAULT_SEARCH_TOP_K = 8
DEFAULT_FETCH_TOP_K = 4
DEFAULT_CACHE_TTL_SECONDS = 86400
MAX_PAGE_TEXT_CHARS = 20000


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class WebConfig:
    enabled: bool = field(default_factory=lambda: _env_bool("SHAMSU_WEB_ENABLED", True))
    auto_start: bool = field(default_factory=lambda: _env_bool("SHAMSU_WEB_AUTO_START", True))
    auto_stop: bool = field(default_factory=lambda: _env_bool("SHAMSU_WEB_AUTO_STOP", True))
    idle_timeout_seconds: int = field(
        default_factory=lambda: _env_int("SHAMSU_WEB_IDLE_TIMEOUT_SECONDS", 600)
    )
    searxng_url: str = field(default_factory=lambda: os.environ.get("SHAMSU_SEARXNG_URL", DEFAULT_SEARXNG_URL))
    provider: str = field(default_factory=lambda: os.environ.get("SHAMSU_WEB_SEARCH_PROVIDER", "auto").lower())
    search_top_k: int = field(default_factory=lambda: _env_int("SHAMSU_WEB_SEARCH_TOP_K", DEFAULT_SEARCH_TOP_K))
    fetch_top_k: int = field(default_factory=lambda: _env_int("SHAMSU_WEB_FETCH_TOP_K", DEFAULT_FETCH_TOP_K))
    cache_enabled: bool = field(default_factory=lambda: _env_bool("SHAMSU_WEB_CACHE_ENABLED", True))
    cache_ttl_seconds: int = field(
        default_factory=lambda: _env_int("SHAMSU_WEB_CACHE_TTL_SECONDS", DEFAULT_CACHE_TTL_SECONDS)
    )


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    source_provider: str = ""


@dataclass(frozen=True)
class WebSearchResult:
    approved: bool
    query: str
    hits: list[SearchHit] = field(default_factory=list)
    error: str = ""
    provider: str = ""
    fallback_used: bool = False
    provider_attempts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class WebFetchResult:
    approved: bool
    url: str
    final_url: str = ""
    title: str = ""
    text: str = ""
    excerpt: str = ""
    source_provider: str = ""
    fetched_at: str = ""
    extraction_method: str = ""
    error: str = ""


@dataclass(frozen=True)
class EvidenceChunk:
    title: str
    url: str
    text: str
    score: float


@dataclass(frozen=True)
class WebSearchFetchResult:
    approved: bool
    query: str
    hits: list[SearchHit] = field(default_factory=list)
    pages: list[WebFetchResult] = field(default_factory=list)
    evidence: list[EvidenceChunk] = field(default_factory=list)
    error: str = ""
    provider: str = ""
    fallback_used: bool = False
    query_type: str = "general"
    provider_attempts: list[dict[str, Any]] = field(default_factory=list)


class WebSearchProvider(Protocol):
    name: str

    def search(self, query: str, top_k: int = DEFAULT_SEARCH_TOP_K) -> list[SearchHit]:
        ...


class SearxngProvider:
    name = "searxng"

    def __init__(self, base_url: str = DEFAULT_SEARXNG_URL, client_factory: Callable[[], httpx.Client] | None = None):
        self.base_url = base_url.rstrip("/")
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=15))

    def search(self, query: str, top_k: int = DEFAULT_SEARCH_TOP_K) -> list[SearchHit]:
        response = self.client_factory().get(
            f"{self.base_url}/search",
            params={"q": query, "format": "json", "categories": "general", "language": "en"},
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        response.raise_for_status()
        return _parse_searxng_results(response.json())[:top_k]


class DuckDuckGoHtmlProvider:
    name = "duckduckgo"

    def __init__(self, client_factory: Callable[[], httpx.Client] | None = None):
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=15))

    def search(self, query: str, top_k: int = DEFAULT_SEARCH_TOP_K) -> list[SearchHit]:
        response = self.client_factory().get(
            f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        response.raise_for_status()
        return _DuckDuckGoParser().parse(response.text)[:top_k]


class QueryPlanner:
    def classify(self, query: str) -> str:
        text = query.lower()
        if any(word in text for word in ("schedule", "fixture", "next game", "kickoff", "utc", "timezone", "time")):
            return "schedule_time"
        if any(word in text for word in ("latest", "current", "today", "now", "price", "weather")):
            return "current_factual"
        if any(word in text for word in ("news", "breaking", "headline")):
            return "news"
        if any(word in text for word in ("docs", "documentation", "api reference", "manual")):
            return "docs"
        if any(word in text for word in ("python", "django", "react", "error", "package", "code", "library")):
            return "coding"
        return "general"


class WebCache:
    def __init__(self, path: Path, ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS, enabled: bool = True) -> None:
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds
        self.enabled = enabled
        self._initialized = False

    def _init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            conn = connection
            conn.execute(
                "CREATE TABLE IF NOT EXISTS web_queries (query TEXT, provider TEXT, fetched_at REAL, hit_count INTEGER)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS web_hits (query TEXT, provider TEXT, title TEXT, url TEXT, snippet TEXT, rank INTEGER, fetched_at REAL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS web_pages (url TEXT PRIMARY KEY, final_url TEXT, title TEXT, text TEXT, excerpt TEXT, source_provider TEXT, fetched_at REAL, extraction_method TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS web_fetch_errors (url TEXT, error TEXT, fetched_at REAL)"
            )
            connection.commit()
            self._initialized = True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_hits(self, query: str, provider: str, hits: list[SearchHit]) -> None:
        if not self.enabled:
            return
        now = time.time()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO web_queries(query, provider, fetched_at, hit_count) VALUES (?, ?, ?, ?)",
                (query, provider, now, len(hits)),
            )
            conn.executemany(
                "INSERT INTO web_hits(query, provider, title, url, snippet, rank, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (query, provider, hit.title, hit.url, hit.snippet, rank, now)
                    for rank, hit in enumerate(hits, start=1)
                ],
            )

    def get_page(self, url: str, refresh: bool = False) -> WebFetchResult | None:
        if not self.enabled or refresh:
            return None
        cutoff = time.time() - self.ttl_seconds
        with self._connection() as conn:
            row = conn.execute(
                "SELECT url, final_url, title, text, excerpt, source_provider, fetched_at, extraction_method "
                "FROM web_pages WHERE url = ? AND fetched_at >= ?",
                (url, cutoff),
            ).fetchone()
        if not row:
            return None
        fetched_at = datetime.fromtimestamp(float(row[6]), timezone.utc).isoformat()
        return WebFetchResult(
            approved=True,
            url=row[0],
            final_url=row[1] or row[0],
            title=row[2] or "",
            text=row[3] or "",
            excerpt=row[4] or "",
            source_provider=row[5] or "cache",
            fetched_at=fetched_at,
            extraction_method=row[7] or "cache",
        )

    def put_page(self, page: WebFetchResult) -> None:
        if not self.enabled or page.error:
            return
        fetched = _timestamp_to_epoch(page.fetched_at) or time.time()
        with self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO web_pages(url, final_url, title, text, excerpt, source_provider, fetched_at, extraction_method) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    page.url,
                    page.final_url or page.url,
                    page.title,
                    page.text,
                    page.excerpt,
                    page.source_provider,
                    fetched,
                    page.extraction_method,
                ),
            )

    def record_error(self, url: str, error: str) -> None:
        if not self.enabled:
            return
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO web_fetch_errors(url, error, fetched_at) VALUES (?, ?, ?)",
                (url, error, time.time()),
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if not self._initialized:
            self._init()
        connection = sqlite3.connect(self.path)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


@dataclass(frozen=True)
class WebServiceStatus:
    ok: bool
    message: str
    running: bool = False
    state: str = "unknown"


@dataclass(frozen=True)
class WebCapabilityStatus:
    enabled: bool
    provider_mode: str
    searxng: WebServiceStatus
    fallback_state: str
    fetch_state: str
    cache_state: str
    cache_path: str

    @property
    def ok(self) -> bool:
        return self.enabled and (
            self.searxng.running or self.fallback_state in {"configured", "available"}
        )


class WebServiceManager:
    project_name = "shamsu-web"
    container_name = "shamsu-searxng"

    def __init__(
        self,
        workspace: Path,
        searxng_url: str = DEFAULT_SEARXNG_URL,
        runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.searxng_url = searxng_url
        self.web_dir = paths.web_dir(self.workspace)
        self.runner = runner or self._run

    @property
    def compose_path(self) -> Path:
        return self.web_dir / "docker-compose.yml"

    def _ensure_valid_settings(self) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if self.settings_path.exists():
            try:
                data = yaml.safe_load(self.settings_path.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}

        if not isinstance(data, dict):
            data = {}

        server = data.get("server")
        if not isinstance(server, dict):
            server = {}
            data["server"] = server

        if not isinstance(server.get("secret_key"), str) or not server["secret_key"].strip():
            server["secret_key"] = secrets.token_urlsafe(48)

        server.setdefault("bind_address", "0.0.0.0")
        server.setdefault("port", 8080)

        search = data.get("search")
        if not isinstance(search, dict):
            search = {}
            data["search"] = search
        search["formats"] = ["html", "json"]

        data.setdefault("use_default_settings", True)

        self.settings_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    
    @property
    def settings_path(self) -> Path:
        return self.web_dir / "searxng" / "settings.yml"

    def setup(self) -> WebServiceStatus:
        self._ensure_valid_settings()
        compose = {
            "services": {
                "searxng": {
                    "image": "searxng/searxng:latest",
                    "container_name": self.container_name,
                    "ports": ["8095:8080"],
                    "volumes": ["./searxng:/etc/searxng:rw"],
                    "labels": ["shamsu.managed=true", "shamsu.service=web-search"],
                    "restart": "unless-stopped",
                }
            }
        }
        self.compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
        return WebServiceStatus(ok=True, message=f"Wrote SearXNG files to {self.web_dir}", state="configured")

    def start(self) -> WebServiceStatus:
        current = self.status()
        if current.running:
            return current
        setup_message = ""
        if not self.compose_path.exists():
            setup = self.setup()
            setup_message = f"{setup.message}\n"
        else:
            self._ensure_valid_settings()
        preflight = self._preflight_start()
        if preflight is not None:
            return WebServiceStatus(ok=False, message=f"{setup_message}{preflight.message}".strip(), running=False, state=preflight.state)
        try:
            result = self.runner(["docker", "compose", "-p", self.project_name, "-f", str(self.compose_path), "up", "-d"])
            failure = self._process_failure(result)
            if failure:
                return WebServiceStatus(ok=False, message=f"{setup_message}{failure}".strip(), running=False, state="failed")
        except Exception as exc:
            return WebServiceStatus(ok=False, message=f"{setup_message}{self._format_process_exception('Docker Compose start failed', exc)}".strip(), running=False, state="failed")
        return self._wait_until_reachable(setup_message=setup_message)

    def stop(self) -> WebServiceStatus:
        if not self._is_managed_container():
            return WebServiceStatus(
                ok=False,
                message="Refusing to stop container because it is not labeled shamsu.managed=true.",
            )
        try:
            self.runner(["docker", "compose", "-p", self.project_name, "-f", str(self.compose_path), "down"])
        except Exception as exc:
            return WebServiceStatus(ok=False, message=f"Docker stop failed: {exc}")
        return WebServiceStatus(ok=True, message="Stopped SHAMSU-managed SearXNG.", running=False, state="stopped")

    def restart(self) -> WebServiceStatus:
        stopped = self.stop()
        if not stopped.ok and "not labeled" not in stopped.message:
            return stopped
        return self.start()

    def status(self) -> WebServiceStatus:
        if not self.compose_path.exists():
            return WebServiceStatus(
                ok=False,
                message=f"SearXNG is not configured. Run /web setup or /web start to create {self.web_dir}.",
                running=False,
                state="not_configured",
            )
        health_error = ""
        try:
            response = httpx.get(f"{self.searxng_url.rstrip('/')}/search", params={"q": "shamsu", "format": "json"}, timeout=3)
            if response.status_code < 500:
                return WebServiceStatus(ok=True, message="SearXNG is reachable.", running=True, state="running")
            health_error = f"Health check returned HTTP {response.status_code}."
        except Exception as exc:
            health_error = f"Health check failed: {exc}"

        container = self._container_status()
        if container:
            return WebServiceStatus(
                ok=False,
                message=f"SearXNG is not reachable. {container} {health_error}",
                running=False,
                state=self._state_from_container_status(container),
            )
        return WebServiceStatus(
            ok=False,
            message=f"SearXNG is configured but not reachable. {health_error} Run /web start for startup diagnostics.",
            running=False,
            state="configured",
        )

    def _is_managed_container(self) -> bool:
        try:
            result = self.runner(
                [
                    "docker",
                    "inspect",
                    self.container_name,
                    "--format",
                    "{{ index .Config.Labels \"shamsu.managed\" }}",
                ]
            )
        except Exception:
            return False
        return result.stdout.strip().lower() == "true"

    def _preflight_start(self) -> WebServiceStatus | None:
        if not shutil.which("docker"):
            return WebServiceStatus(
                ok=False,
                message=(
                    "Docker is not installed or is not on PATH. Install Docker Desktop, start it, "
                    "then run /web start again."
                ),
                state="missing_docker",
            )
        compose = self._run_checked(["docker", "compose", "version"], "Docker Compose is not available")
        if compose:
            return WebServiceStatus(ok=False, message=compose, state="missing_compose")
        daemon = self._run_checked(["docker", "info"], "Docker is installed but the daemon is not running")
        if daemon:
            return WebServiceStatus(
                ok=False,
                message=f"{daemon}\nStart Docker Desktop, then run /web start again.",
                state="docker_not_running",
            )
        port_conflict = self._port_conflict_message()
        if port_conflict:
            return WebServiceStatus(ok=False, message=port_conflict, state="port_conflict")
        return None

    def _run_checked(self, command: list[str], description: str) -> str:
        try:
            result = self.runner(command)
        except Exception as exc:
            return self._format_process_exception(description, exc)
        failure = self._process_failure(result)
        return f"{description}: {failure}" if failure else ""

    def _process_failure(self, result: Any) -> str:
        returncode = getattr(result, "returncode", 0)
        if returncode in (0, None):
            return ""
        output = "\n".join(
            part.strip()
            for part in (getattr(result, "stderr", ""), getattr(result, "stdout", ""))
            if str(part or "").strip()
        )
        if output:
            return self._classify_docker_error(output)
        return f"command exited with code {returncode}"

    def _format_process_exception(self, description: str, exc: Exception) -> str:
        if isinstance(exc, subprocess.CalledProcessError):
            output = "\n".join(
                part.strip()
                for part in (exc.stderr, exc.stdout)
                if str(part or "").strip()
            )
            return f"{description}: {self._classify_docker_error(output or str(exc))}"
        if isinstance(exc, FileNotFoundError):
            return f"{description}: docker executable was not found. Install Docker Desktop and ensure docker is on PATH."
        return f"{description}: {exc}"

    def _classify_docker_error(self, output: str) -> str:
        lowered = output.lower()
        if "port is already allocated" in lowered or "bind" in lowered and "8095" in lowered:
            return f"port conflict on {self.searxng_url}; Docker could not bind the SearXNG port.\n{output}"
        if "cannot connect to the docker daemon" in lowered or "docker daemon" in lowered:
            return f"Docker daemon is not running.\n{output}"
        if "compose" in lowered and ("not a docker command" in lowered or "unknown command" in lowered):
            return f"Docker Compose is not available.\n{output}"
        return output

    def _wait_until_reachable(self, setup_message: str = "") -> WebServiceStatus:
        deadline = time.monotonic() + 20
        last = self.status()
        while time.monotonic() < deadline:
            last = self.status()
            if last.running:
                message = f"{setup_message}{last.message}".strip()
                return WebServiceStatus(ok=True, message=message, running=True, state="running")
            time.sleep(1)
        diagnostic = self._container_status() or "Container status was unavailable."
        logs = self._recent_logs()
        message = (
            f"{setup_message}Started Docker Compose, but SearXNG did not become healthy at {self.searxng_url}.\n"
            f"{last.message}\n"
            f"Container: {diagnostic}"
        ).strip()
        if logs:
            message = f"{message}\nRecent logs:\n{logs}"
        return WebServiceStatus(ok=False, message=message, running=False, state="healthcheck_failed")

    def _container_status(self) -> str:
        try:
            result = self.runner(
                [
                    "docker",
                    "inspect",
                    self.container_name,
                    "--format",
                    "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} {{.State.Error}}",
                ]
            )
        except Exception:
            return ""
        if self._process_failure(result):
            return ""
        return " ".join(getattr(result, "stdout", "").split())

    def _state_from_container_status(self, status: str) -> str:
        lowered = status.lower()
        if "running" in lowered and "healthy" in lowered:
            return "healthcheck_failed"
        if "running" in lowered:
            return "starting"
        if "exited" in lowered or "dead" in lowered:
            return "failed"
        if "created" in lowered:
            return "stopped"
        return "configured"

    def _recent_logs(self) -> str:
        try:
            result = self.runner(["docker", "logs", "--tail", "40", self.container_name])
        except Exception:
            return ""
        if self._process_failure(result):
            return ""
        text = "\n".join(
            part.strip()
            for part in (getattr(result, "stderr", ""), getattr(result, "stdout", ""))
            if str(part or "").strip()
        )
        return text[-4000:]

    def _port_conflict_message(self) -> str:
        parsed = urlparse(self.searxng_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if host not in {"localhost", "127.0.0.1", "::1"}:
            return ""
        try:
            with socket.create_connection((host, port), timeout=1):
                return (
                    f"Port {port} on {host} is already accepting connections, but the SearXNG health check failed. "
                    "Stop the conflicting service or change SHAMSU_SEARXNG_URL/compose port, then run /web start again."
                )
        except OSError:
            return ""

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, cwd=self.web_dir, text=True, capture_output=True, check=True)


class WebTool:
    def __init__(
        self,
        approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
        session_logger: SessionLogger | None = None,
        timeout_seconds: int = 15,
        approval_manager: ApprovalManager | None = None,
        workspace: Path | None = None,
        config: WebConfig | None = None,
        action_ledger: Any | None = None,
    ) -> None:
        self.approval_func = approval_func
        self.approval_manager = approval_manager or ApprovalManager(approval_func, session_logger)
        self.session_logger = session_logger
        self.timeout_seconds = timeout_seconds
        self.workspace = Path(workspace or Path.cwd())
        self.config = config or WebConfig()
        self.action_ledger = action_ledger
        self.service_manager = WebServiceManager(self.workspace, self.config.searxng_url)
        self.cache = WebCache(
            paths.web_cache_db(self.workspace),
            ttl_seconds=self.config.cache_ttl_seconds,
            enabled=self.config.cache_enabled,
        )
        self.query_planner = QueryPlanner()
        self._provider_attempts: list[dict[str, Any]] = []

    def status(self) -> WebCapabilityStatus:
        searxng = self.service_manager.status()
        fallback_configured = self.config.provider in {"auto", "duckduckgo"}
        return WebCapabilityStatus(
            enabled=self.config.enabled,
            provider_mode=self.config.provider,
            searxng=searxng,
            fallback_state="configured" if fallback_configured and self.config.enabled else "disabled",
            fetch_state="configured_not_probed" if self.config.enabled else "disabled",
            cache_state=(
                "enabled" if self.config.cache_enabled else "disabled"
            ),
            cache_path=str(self.cache.path),
        )

    def search(self, query: str, reason: str = "", top_k: int = 5) -> WebSearchResult:
        if not self.config.enabled:
            return WebSearchResult(approved=False, query=query, error="Web search is disabled by SHAMSU_WEB_ENABLED=false.")
        validation_error = self._validate_query(query)
        if validation_error:
            self._log("web.request.blocked", {"query": query, "error": validation_error}, "Blocked unsafe external query")
            return WebSearchResult(approved=False, query=query, error=validation_error)
        request = ApprovalRequest(
            action_type="web_search",
            description="Search the web for current or external information.",
            risk_level="medium",
            preview=query,
            reason=reason or "This request appears to need external knowledge.",
        )
        self._log("web.search.requested", {"query": query, "reason": reason}, f"Requested web search: {query}")
        self.approval_manager.session_logger = self.session_logger
        if not self.approval_manager.ask(request):
            self._log("web.search.denied", {"query": query}, f"Denied web search: {query}")
            return WebSearchResult(approved=False, query=query, error="Web search denied by user.")

        self._log("web.search.started", {"query": query}, f"Started web search: {query}")
        self._provider_attempts = []
        try:
            hits, provider, fallback_used = self._run_provider_search(query, top_k)
            hits = _rank_authoritative_hits(query, _dedupe_hits(hits))
            self.cache.record_hits(query, provider, hits)
            self._log(
                "web.search.finished",
                {
                    "query": query,
                    "provider": provider,
                    "fallback_used": fallback_used,
                    "hit_count": len(hits),
                    "results": _ranked_hit_records(hits),
                    "provider_attempts": list(self._provider_attempts),
                },
                f"Finished web search: {query}",
            )
            return WebSearchResult(
                approved=True,
                query=query,
                hits=hits,
                provider=provider,
                fallback_used=fallback_used,
                provider_attempts=list(self._provider_attempts),
            )
        except Exception as exc:
            message = str(exc)
            self._log("web.search.failed", {"query": query, "error": message}, f"Failed web search: {query}")
            return WebSearchResult(
                approved=True,
                query=query,
                error=message,
                provider_attempts=list(self._provider_attempts),
            )

    def fetch(self, url: str, reason: str = "", require_approval: bool = True) -> WebFetchResult:
        if not self.config.enabled:
            return WebFetchResult(approved=False, url=url, error="Web search is disabled by SHAMSU_WEB_ENABLED=false.")
        validation_error = _validate_external_url(url)
        if validation_error:
            self._log("web.request.blocked", {"url": url, "error": validation_error}, "Blocked unsafe external URL")
            return WebFetchResult(approved=False, url=url, error=validation_error)
        if require_approval:
            request = ApprovalRequest(
                action_type="web_search",
                description="Fetch and read a web page.",
                risk_level="medium",
                preview=url,
                reason=reason or "SHAMSU wants to inspect an external page for this request.",
            )
            self._log("web.fetch.requested", {"url": url, "reason": reason}, f"Requested fetch: {url}")
            self.approval_manager.session_logger = self.session_logger
            if not self.approval_manager.ask(request):
                self._log("web.fetch.denied", {"url": url}, f"Denied fetch: {url}")
                return WebFetchResult(approved=False, url=url, error="Web fetch denied by user.")

        self._log("web.fetch.started", {"url": url}, f"Started fetch: {url}")
        try:
            response = self._fetch_public_response(url)
            response.raise_for_status()
            extracted, method = _extract_readable_text(response.text, str(response.url))
            title_parser = _VisibleTextParser()
            title_parser.feed(response.text)
            if extracted:
                text = _normalize_extracted_text(extracted)[:MAX_PAGE_TEXT_CHARS]
            else:
                text = title_parser.text()[:MAX_PAGE_TEXT_CHARS]
                method = "visible_text"
            title = title_parser.title.strip() or _hostname(url)
            page = WebFetchResult(
                approved=True,
                url=url,
                final_url=str(response.url),
                title=title,
                text=text,
                excerpt=text[:600],
                source_provider=_hostname(url),
                fetched_at=_now_iso(),
                extraction_method=method,
            )
            self.cache.put_page(page)
            self._log(
                "web.fetch.finished",
                {
                    "url": url,
                    "final_url": page.final_url,
                    "title": title,
                    "text_length": len(text),
                    "extraction_method": method,
                    "fetched_at": page.fetched_at,
                    "source_provider": page.source_provider,
                },
                f"Finished fetch: {url}",
            )
            return page
        except Exception as exc:
            message = str(exc)
            self.cache.record_error(url, message)
            self._log("web.fetch.failed", {"url": url, "error": message}, f"Failed fetch: {url}")
            return WebFetchResult(approved=True, url=url, fetched_at=_now_iso(), error=message)

    def search_and_fetch(
        self,
        query: str,
        reason: str = "",
        search_top_k: int | None = None,
        fetch_top_k: int | None = None,
        require_local_service: bool = False,
    ) -> WebSearchFetchResult:
        if not self.config.enabled:
            return WebSearchFetchResult(
                approved=False,
                query=query,
                error="Web search is disabled by SHAMSU_WEB_ENABLED=false.",
            )
        validation_error = self._validate_query(query)
        if validation_error:
            self._log("web.request.blocked", {"query": query, "error": validation_error}, "Blocked unsafe external query")
            return WebSearchFetchResult(approved=False, query=query, error=validation_error)
        search_top_k = search_top_k or self.config.search_top_k
        fetch_top_k = fetch_top_k or self.config.fetch_top_k
        query_type = self.query_planner.classify(query)
        request = ApprovalRequest(
            action_type="web_search",
            description="Search the web and fetch the top results.",
            risk_level="medium",
            preview=query,
            reason=reason or "This request needs sourced external evidence.",
        )
        self._log(
            "web.search_and_fetch.requested",
            {"query": query, "query_type": query_type, "search_top_k": search_top_k, "fetch_top_k": fetch_top_k},
            f"Requested web evidence search: {query}",
        )
        self.approval_manager.session_logger = self.session_logger
        if not self.approval_manager.ask(request):
            self._log("web.search_and_fetch.denied", {"query": query}, f"Denied web evidence search: {query}")
            return WebSearchFetchResult(approved=False, query=query, error="Web search denied by user.", query_type=query_type)

        if require_local_service:
            try:
                self._ensure_searxng_ready()
            except Exception as exc:
                message = str(exc)
                self._log("web.search_and_fetch.failed", {"query": query, "error": message}, f"Failed web evidence search: {query}")
                return WebSearchFetchResult(approved=True, query=query, error=message, query_type=query_type)

        self._provider_attempts = []
        try:
            hits, provider, fallback_used = self._run_provider_search(query, search_top_k)
            hits = _rank_authoritative_hits(query, _dedupe_hits(hits))
            self.cache.record_hits(query, provider, hits)
        except Exception as exc:
            message = str(exc)
            self._log("web.search_and_fetch.failed", {"query": query, "error": message}, f"Failed web evidence search: {query}")
            return WebSearchFetchResult(approved=True, query=query, error=message, query_type=query_type)

        pages: list[WebFetchResult] = []
        refresh = _query_requests_freshness(query, query_type)
        for hit in hits[:fetch_top_k]:
            cached = self.cache.get_page(hit.url, refresh=refresh)
            if cached:
                page = WebFetchResult(
                    **{**cached.__dict__, "source_provider": cached.source_provider or provider}
                )
                pages.append(page)
                self._log("web.fetch.cache_hit", {"url": hit.url}, f"Used cached web page: {hit.url}")
                continue
            page = self.fetch(
                hit.url,
                reason="SHAMSU already has approval to search and read the top results.",
                require_approval=False,
            )
            if page.approved and not page.error:
                pages.append(page)

        evidence = select_evidence(query, hits, pages)
        self._log(
            "web.search_and_fetch.finished",
            {
                "query": query,
                "provider": provider,
                "fallback_used": fallback_used,
                "hit_count": len(hits),
                "page_count": len(pages),
                "evidence_count": len(evidence),
                "query_type": query_type,
                "results": _ranked_hit_records(hits),
                "provider_attempts": list(self._provider_attempts),
            },
            f"Finished web evidence search: {query}",
        )
        return WebSearchFetchResult(
            approved=True,
            query=query,
            hits=hits,
            pages=pages,
            evidence=evidence,
            provider=provider,
            fallback_used=fallback_used,
            query_type=query_type,
            provider_attempts=list(self._provider_attempts),
        )

    def _validate_query(self, query: str) -> str:
        cleaned = query.strip()
        if not cleaned:
            return "Web search needs a non-empty query."
        if len(cleaned) > 1000:
            return "External search query exceeds the 1000-character privacy limit."
        workspace_text = str(self.workspace.resolve()).lower()
        lowered = cleaned.lower()
        if workspace_text in lowered or ".shamsu" in lowered:
            return "External search query contains a private workspace path."
        return ""

    def _fetch_public_response(self, url: str):
        current = url
        client = self._client()
        for _redirect in range(6):
            error = _validate_external_url(current)
            if error:
                raise ValueError(error)
            response = client.get(
                current,
                headers={"User-Agent": DEFAULT_USER_AGENT},
                follow_redirects=False,
            )
            if not getattr(response, "is_redirect", False):
                return response
            location = response.headers.get("location", "")
            if not location:
                return response
            current = urljoin(current, location)
        raise RuntimeError("Web fetch exceeded five redirects.")

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout_seconds)

    def _log(self, event_type: str, payload: dict, summary: str) -> None:
        if self.session_logger:
            self.session_logger.log(event_type, payload, summary, workflow_id="web")
        if self.action_ledger:
            try:
                self.action_ledger.log_event(event_type.replace(".", "_"), **payload)
            except Exception:
                pass

    def _run_provider_search(self, query: str, top_k: int) -> tuple[list[SearchHit], str, bool]:
        provider_name = self.config.provider
        self._log("web.provider.selected", {"provider": provider_name, "query": query}, f"Selected web provider: {provider_name}")
        if provider_name == "duckduckgo":
            provider = DuckDuckGoHtmlProvider(client_factory=self._client)
            return self._run_one_provider(provider, query, top_k), provider.name, False
        if provider_name == "searxng":
            try:
                self._ensure_searxng_ready()
                provider = SearxngProvider(self.config.searxng_url, client_factory=self._client)
                return self._run_one_provider(provider, query, top_k), provider.name, False
            except Exception as exc:
                self._record_provider_attempt("searxng", "failed", str(exc))
                raise

        errors: list[str] = []
        try:
            self._ensure_searxng_ready()
            provider = SearxngProvider(self.config.searxng_url, client_factory=self._client)
            return self._run_one_provider(provider, query, top_k), provider.name, False
        except Exception as exc:
            errors.append(f"SearXNG failed: {exc}")
            self._record_provider_attempt("searxng", "failed", str(exc))
            self._log("web.provider.fallback", {"from": "searxng", "to": "duckduckgo", "error": str(exc)}, "Fell back to DuckDuckGo web search")
        provider = DuckDuckGoHtmlProvider(client_factory=self._client)
        try:
            return self._run_one_provider(provider, query, top_k), provider.name, True
        except Exception as exc:
            errors.append(f"DuckDuckGo failed: {exc}")
            raise RuntimeError("; ".join(errors)) from exc

    def _run_one_provider(
        self,
        provider: WebSearchProvider,
        query: str,
        top_k: int,
    ) -> list[SearchHit]:
        effective_query = _simplify_search_query(query) or query
        if effective_query != query:
            self._log(
                "web.query.simplified",
                {"provider": provider.name, "original_query": query, "query": effective_query},
                "Simplified conversational web request for the search provider",
            )
        try:
            hits = _with_provider(provider.search(effective_query, top_k), provider.name)
        except Exception as exc:
            self._record_provider_attempt(provider.name, "failed", str(exc))
            raise
        self._record_provider_attempt(provider.name, "success", "", len(hits))
        return hits

    def _record_provider_attempt(
        self,
        provider: str,
        state: str,
        error: str = "",
        hit_count: int = 0,
    ) -> None:
        comparable = {"provider": provider, "state": state, "error": error}
        if self._provider_attempts:
            previous = self._provider_attempts[-1]
            previous_comparable = {
                "provider": previous.get("provider"),
                "state": previous.get("state"),
                "error": previous.get("error", ""),
            }
            if previous_comparable == comparable:
                return
        item = {
            "provider": provider,
            "state": state,
            "hit_count": hit_count,
            "retrieved_at": _now_iso(),
        }
        if error:
            item["error"] = error
        if item not in self._provider_attempts:
            self._provider_attempts.append(item)

    def _ensure_searxng_ready(self) -> None:
        status = self.service_manager.status()
        if status.running:
            return
        if not self.config.auto_start:
            raise RuntimeError("SearXNG is not running and SHAMSU_WEB_AUTO_START is disabled.")
        started = self.service_manager.start()
        if not started.ok:
            raise RuntimeError(started.message)


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[SearchHit] = []
        self._in_link = False
        self._capture_snippet = False
        self._current_href = ""
        self._current_title: list[str] = []
        self._current_snippet: list[str] = []

    def parse(self, html: str) -> list[SearchHit]:
        self.feed(html)
        return self.results

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        classes = attributes.get("class", "")
        if tag == "a" and "result__a" in classes:
            self._in_link = True
            self._current_href = attributes.get("href", "")
            self._current_title = []
            self._current_snippet = []
        elif tag in {"a", "div"} and "result__snippet" in classes:
            self._capture_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_link:
            title = _normalize_space("".join(self._current_title))
            href = _normalize_ddg_href(self._current_href)
            if title and href:
                self.results.append(SearchHit(title=title, url=href, snippet=""))
            self._in_link = False
        elif self._capture_snippet and tag in {"a", "div"}:
            if self.results:
                last = self.results[-1]
                self.results[-1] = SearchHit(
                    last.title,
                    last.url,
                    _normalize_space("".join(self._current_snippet)),
                )
            self._current_snippet = []
            self._capture_snippet = False

    def handle_data(self, data: str) -> None:
        if self._in_link:
            self._current_title.append(data)
        elif self._capture_snippet:
            self._current_snippet.append(data)


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []

    @property
    def title(self) -> str:
        return _normalize_space("".join(self._title_parts))

    def text(self) -> str:
        return _normalize_space(" ".join(self._parts))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._skip_depth:
            return
        cleaned = _normalize_space(data)
        if cleaned:
            self._parts.append(cleaned)


def _normalize_space(text: str) -> str:
    return " ".join(unescape(text).split())


def _normalize_extracted_text(text: str) -> str:
    lines = [unescape(line).strip() for line in text.splitlines()]
    compact: list[str] = []
    previous_blank = False
    for line in lines:
        if not line:
            if not previous_blank:
                compact.append("")
            previous_blank = True
            continue
        compact.append(re.sub(r"[ \t]+", " ", line))
        previous_blank = False
    return "\n".join(compact).strip()


def _normalize_ddg_href(href: str) -> str:
    href = unescape(href).strip()
    if not href:
        return ""
    if href.startswith("//"):
        href = f"https:{href}"
    elif href.startswith("/"):
        href = f"https://duckduckgo.com{href}"

    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path == "/l/":
        uddg = parse_qs(parsed.query).get("uddg", [""])[0]
        if uddg:
            return unquote(uddg)
    return href


def _simplify_search_query(query: str) -> str:
    """Turn a conversational web request into a search-engine query."""
    simplified = " ".join(query.split()).strip()
    simplified = re.sub(
        r"^(?:please\s+)?(?:use\s+(?:the\s+)?web\s+search\s+to\s+|"
        r"search\s+(?:the\s+)?web\s+(?:for|to\s+find)\s+|"
        r"browse\s+(?:the\s+)?web\s+(?:for|to\s+find)\s+)",
        "",
        simplified,
        flags=re.IGNORECASE,
    )
    simplified = re.sub(
        r"^(?:find|look\s+up|search\s+for)\s+", "", simplified, flags=re.IGNORECASE
    )
    simplified = re.split(
        r"(?<=[.!?])\s+(?=(?:give|return|include|cite|tell|report|do\s+not|don't|please)\b)",
        simplified,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return simplified.strip(" \t\r\n.!?")


def _extract_readable_text(html: str, url: str) -> tuple[str | None, str]:
    global trafilatura
    if trafilatura is _TRAFILATURA_NOT_LOADED:
        try:
            import trafilatura as trafilatura_module
        except ModuleNotFoundError:  # pragma: no cover - old/global launcher missing deps
            trafilatura = None
        else:
            trafilatura = trafilatura_module
    if trafilatura is None:
        return None, "none"
    extracted = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )
    return extracted, "trafilatura_markdown" if extracted else "trafilatura_empty"


def _hostname(url: str) -> str:
    parsed = urlparse(url)
    return parsed.hostname or url


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_to_epoch(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _parse_searxng_results(payload: dict[str, Any]) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for item in payload.get("results", []) or []:
        url = str(item.get("url") or "").strip()
        title = _normalize_space(str(item.get("title") or ""))
        snippet = _normalize_space(str(item.get("content") or item.get("snippet") or ""))
        if title and url:
            hits.append(SearchHit(title=title, url=url, snippet=snippet, source_provider="searxng"))
    return hits


def _validate_external_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return "Web fetch only supports public HTTP or HTTPS URLs."
    if not parsed.hostname:
        return "Web fetch URL is missing a hostname."
    if parsed.username or parsed.password:
        return "Web fetch URLs cannot contain credentials."
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
        return "Use the browser tool, not web fetch, for local applications."
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return ""
    if address.is_loopback:
        return "Use the browser tool, not web fetch, for local applications."
    if not address.is_global:
        return "Web fetch cannot access private, loopback, link-local, or reserved addresses."
    return ""


def _ranked_hit_records(hits: list[SearchHit]) -> list[dict[str, Any]]:
    retrieved_at = _now_iso()
    return [
        {
            "rank": rank,
            "title": hit.title,
            "url": hit.url,
            "provider": hit.source_provider,
            "retrieved_at": retrieved_at,
        }
        for rank, hit in enumerate(hits, start=1)
    ]


def _with_provider(hits: list[SearchHit], provider: str) -> list[SearchHit]:
    return [
        SearchHit(title=hit.title, url=hit.url, snippet=hit.snippet, source_provider=hit.source_provider or provider)
        for hit in hits
    ]


def _dedupe_hits(hits: list[SearchHit]) -> list[SearchHit]:
    seen: set[str] = set()
    unique: list[SearchHit] = []
    for hit in hits:
        key = hit.url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
    return unique


def _query_requests_freshness(query: str, query_type: str) -> bool:
    text = query.lower()
    return query_type in {"current_factual", "news", "schedule_time"} or any(
        word in text for word in ("latest", "current", "today", "now", "next")
    )


def _keywords(text: str) -> set[str]:
    stop = {"the", "and", "for", "with", "from", "what", "when", "where", "this", "that", "into", "time"}
    return {word for word in re.findall(r"[a-z0-9]{3,}", text.lower()) if word not in stop}


def select_evidence(query: str, hits: list[SearchHit], pages: list[WebFetchResult], limit: int = 6) -> list[EvidenceChunk]:
    query_words = _keywords(query)
    chunks: list[EvidenceChunk] = []
    for page in pages:
        if not page.text.strip():
            continue
        for chunk in _chunk_text(page.text, size=1400, overlap=180):
            score = _score_text(query_words, page.title, chunk, page.url)
            chunks.append(EvidenceChunk(title=page.title or page.url, url=page.final_url or page.url, text=chunk, score=score))
    if not chunks:
        for hit in hits:
            text = f"{hit.title}\n{hit.snippet}".strip()
            if text:
                chunks.append(EvidenceChunk(title=hit.title, url=hit.url, text=text, score=_score_text(query_words, hit.title, text, hit.url)))
    chunks.sort(key=lambda item: item.score, reverse=True)
    return chunks[:limit]


def _chunk_text(text: str, size: int, overlap: int) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        chunks.append(text[start:end].strip())
        if end == len(text):
            break
        start = max(0, end - overlap)
    return chunks


def _score_text(query_words: set[str], title: str, text: str, url: str) -> float:
    haystack = _keywords(f"{title} {text}")
    overlap = len(query_words & haystack)
    title_overlap = len(query_words & _keywords(title)) * 1.5
    domain_bonus = 1.0 if any(domain in url for domain in ("docs.", ".gov", ".edu", "wikipedia.org", "github.com")) else 0.0
    recency_bonus = 0.5 if any(word in text.lower() for word in ("latest", "updated", "2026", "today")) else 0.0
    return overlap + title_overlap + domain_bonus + recency_bonus


def _rank_authoritative_hits(query: str, hits: list[SearchHit]) -> list[SearchHit]:
    """Prefer first-party documentation when the user explicitly asks for it."""
    text = query.lower()
    preferred: tuple[str, ...] = ()
    if "python" in text:
        preferred = ("python.org",)
    elif "node.js" in text or "nodejs" in text:
        preferred = ("nodejs.org",)
    elif "django" in text:
        preferred = ("djangoproject.com",)
    elif "react" in text:
        preferred = ("react.dev",)
    if "official" not in text and not preferred:
        return hits

    def score(hit: SearchHit) -> tuple[int, int]:
        host = urlparse(hit.url).hostname or ""
        first_party = int(any(host == domain or host.endswith("." + domain) for domain in preferred))
        docs_like = int(host.startswith("docs.") or "/docs" in hit.url or "/download" in hit.url)
        return first_party, docs_like

    return sorted(hits, key=score, reverse=True)


def build_evidence_answer_prompt(query: str, result: WebSearchFetchResult) -> str:
    evidence = result.evidence or select_evidence(query, result.hits, result.pages)
    evidence_text = "\n\n".join(
        f"[{index}] {chunk.title}\nURL: {chunk.url}\nEvidence:\n{chunk.text}"
        for index, chunk in enumerate(evidence, start=1)
    )
    searched = "\n".join(f"- {hit.title}: {hit.url}" for hit in result.hits[:8])
    fetched = "\n".join(
        f"- {page.title or page.url}: {page.final_url or page.url} ({page.extraction_method or 'unknown'})"
        for page in result.pages
    )
    missing = "" if result.pages else "No readable fetched page evidence was available; do not answer factual/current claims from snippets alone."
    return (
        "Answer only from fetched evidence below. If the evidence does not contain the answer, say that SHAMSU could not verify it. "
        "Do not guess or fill gaps from snippets/general knowledge. Preserve the exact entity, version, and event in the user's question. "
        "For an unqualified major/minor software version release date, answer with that version's initial final release "
        "(usually x.y.0), not a later maintenance, bugfix, security, or end-of-life date. If evidence mentions several "
        "release types, label them explicitly and do not substitute one for another. Mention uncertainty. Include source titles and URLs.\n\n"
        f"User question: {query}\n"
        f"Query type: {result.query_type}\n"
        f"Provider: {result.provider or 'unknown'}{' (fallback used)' if result.fallback_used else ''}\n\n"
        f"Evidence chunks:\n{evidence_text or '(none)'}\n\n"
        f"Sources fetched:\n{fetched or '(none)'}\n\n"
        f"Sources searched:\n{searched or '(none)'}\n\n"
        f"What may be missing: {missing or 'State any missing evidence yourself if the chunks are incomplete.'}\n\n"
        "Give one direct, internally consistent answer. Reconcile conflicting evidence by preferring "
        "first-party sources and the newest final/stable release. Do not add a source list; the harness "
        "will append the exact fetched URLs. Briefly state uncertainty only when the fetched evidence "
        "does not resolve it."
    )
