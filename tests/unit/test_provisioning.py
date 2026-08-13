"""Starting Ollama and fetching a model, without doing either.

Every test here drives `provisioning` against a stubbed server or stubbed
`subprocess`. `tests/conftest.py` says no live model, ever — and that has to
hold most firmly for the module whose entire job is to go and get one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from shamsu.models import provisioning
from shamsu.models.provisioning import (
    NotProvisionable,
    Progress,
    ensure_ready,
    has_model,
    pull_model,
    server_is_up,
)

HOST = "http://localhost:11434"


def _tags(*names: str) -> httpx.Response:
    return httpx.Response(200, json={"models": [{"name": name} for name in names]})


class TestServerDetection:
    def test_a_responding_server_is_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(httpx.Client, "get", lambda self, url: _tags("a:1"))
        assert server_is_up(HOST) is True

    def test_a_refused_connection_is_not(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(self: httpx.Client, url: str) -> httpx.Response:
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx.Client, "get", refuse)
        assert server_is_up(HOST) is False


class TestStartingTheServer:
    def test_an_absent_ollama_says_how_to_install_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The one situation this module must not paper over."""
        monkeypatch.setattr(provisioning, "server_is_up", lambda *a, **k: False)
        monkeypatch.setattr(provisioning, "ollama_executable", lambda: None)

        with pytest.raises(NotProvisionable, match="not installed"):
            provisioning.start_server(HOST)

    def test_an_already_running_server_is_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The common case, and it must cost nothing."""
        spawned: list[list[str]] = []
        monkeypatch.setattr(provisioning, "server_is_up", lambda *a, **k: True)
        monkeypatch.setattr(provisioning, "_spawn_detached", lambda argv: spawned.append(argv))

        provisioning.start_server(HOST)
        assert spawned == []

    def test_it_spawns_ollama_serve_and_waits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spawned: list[list[str]] = []
        answers = iter([False, False, True])
        monkeypatch.setattr(provisioning, "ollama_executable", lambda: "/usr/bin/ollama")
        monkeypatch.setattr(provisioning, "_spawn_detached", lambda argv: spawned.append(argv))
        monkeypatch.setattr(provisioning, "POLL_SECONDS", 0.0)
        monkeypatch.setattr(provisioning, "server_is_up", lambda *a, **k: next(answers))

        seen: list[Progress] = []
        provisioning.start_server(HOST, on_progress=seen.append)

        assert spawned == [["/usr/bin/ollama", "serve"]]
        assert any("server is up" in progress.message for progress in seen)

    def test_a_server_that_never_answers_times_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bounded: a broken install must not hang the command forever."""
        monkeypatch.setattr(provisioning, "ollama_executable", lambda: "/usr/bin/ollama")
        monkeypatch.setattr(provisioning, "_spawn_detached", lambda argv: None)
        monkeypatch.setattr(provisioning, "POLL_SECONDS", 0.0)
        monkeypatch.setattr(provisioning, "server_is_up", lambda *a, **k: False)

        with pytest.raises(NotProvisionable, match="did not answer"):
            provisioning.start_server(HOST, timeout=0.05)


class TestModelDetection:
    def test_an_exact_tag_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            provisioning, "list_models", lambda host: ("qwen2.5-coder:7b-instruct-q4_K_M",)
        )
        assert has_model("qwen2.5-coder:7b-instruct-q4_K_M", HOST) is True

    def test_an_untagged_name_matches_latest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Otherwise a pulled model is reported missing and downloaded twice."""
        monkeypatch.setattr(provisioning, "list_models", lambda host: ("mistral:latest",))
        assert has_model("mistral", HOST) is True

    def test_a_different_quant_is_a_different_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(provisioning, "list_models", lambda host: ("qwen2.5-coder:7b",))
        assert has_model("qwen2.5-coder:7b-instruct-q4_K_M", HOST) is False


def _stream(*events: dict[str, Any]) -> Any:
    """Stand in for `_pull_events`, yielding decoded updates."""

    def events_for(model: str, host: str, stall: float) -> Iterator[dict[str, Any]]:
        yield from events

    return events_for


class TestPulling:
    def test_progress_is_reported_with_a_fraction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            provisioning,
            "_pull_events",
            _stream(
                {"status": "pulling manifest"},
                {"status": "downloading", "total": 100, "completed": 25},
                {"status": "success"},
            ),
        )
        monkeypatch.setattr(provisioning, "has_model", lambda model, host=HOST: True)

        seen: list[Progress] = []
        pull_model("m", HOST, on_progress=seen.append)

        assert any(progress.fraction == 0.25 for progress in seen)
        assert "25%" in next(p.render() for p in seen if p.fraction == 0.25)

    def test_an_error_event_stops_the_pull(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            provisioning, "_pull_events", _stream({"status": "x", "error": "no such model"})
        )
        with pytest.raises(NotProvisionable, match="no such model"):
            pull_model("nope", HOST)

    def test_an_error_without_a_status_still_stops_the_pull(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shape Ollama actually sends for a bad model name.

        Found live: the error check sat after a `continue` that skipped events
        with no `status`, so the only explanation available was discarded and
        the pull failed with a vague fallback instead.
        """
        monkeypatch.setattr(
            provisioning,
            "_pull_events",
            _stream(
                {"status": "pulling manifest"},
                {"error": "pull model manifest: file does not exist"},
            ),
        )
        with pytest.raises(NotProvisionable, match="file does not exist"):
            pull_model("nope:v999", HOST)

    def test_a_pull_that_changes_nothing_is_not_a_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reporting success for a model that is still absent would be a lie."""
        monkeypatch.setattr(provisioning, "_pull_events", _stream({"status": "success"}))
        monkeypatch.setattr(provisioning, "has_model", lambda model, host=HOST: False)

        with pytest.raises(NotProvisionable, match="still not"):
            pull_model("m", HOST)


class TestEnsureReady:
    def test_the_common_case_does_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(provisioning, "server_is_up", lambda *a, **k: True)
        monkeypatch.setattr(provisioning, "has_model", lambda model, host=HOST: True)
        monkeypatch.setattr(provisioning, "start_server", lambda *a, **k: calls.append("start"))
        monkeypatch.setattr(provisioning, "pull_model", lambda *a, **k: calls.append("pull"))

        ensure_ready("m", HOST)
        assert calls == []

    def test_it_starts_then_pulls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(provisioning, "server_is_up", lambda *a, **k: False)
        monkeypatch.setattr(provisioning, "has_model", lambda model, host=HOST: False)
        monkeypatch.setattr(provisioning, "start_server", lambda *a, **k: calls.append("start"))
        monkeypatch.setattr(provisioning, "pull_model", lambda *a, **k: calls.append("pull"))

        ensure_ready("m", HOST)
        assert calls == ["start", "pull"]

    def test_offline_refuses_to_start_and_says_what_to_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provisioning, "server_is_up", lambda *a, **k: False)

        with pytest.raises(NotProvisionable, match="ollama serve"):
            ensure_ready("m", HOST, start=False)

    def test_offline_refuses_to_pull_and_lists_what_is_there(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provisioning, "server_is_up", lambda *a, **k: True)
        monkeypatch.setattr(provisioning, "has_model", lambda model, host=HOST: False)
        monkeypatch.setattr(provisioning, "list_models", lambda host: ("gemma3:4b",))

        with pytest.raises(NotProvisionable, match="gemma3:4b"):
            ensure_ready("m", HOST, pull=False)


class TestTheEventStreamIsParsedNotGuessed:
    def test_blank_and_malformed_lines_are_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = "\n".join(["", "not json", json.dumps({"status": "success"}), ""])

        class _Response:
            status_code = 200

            def iter_lines(self) -> Iterator[str]:
                yield from body.splitlines()

            def read(self) -> bytes:  # pragma: no cover - not reached at 200
                return b""

            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(httpx.Client, "stream", lambda self, *a, **k: _Response())
        updates = list(provisioning._pull_events("m", HOST, 5.0))
        assert updates == [{"status": "success"}]
