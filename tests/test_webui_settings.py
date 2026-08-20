"""The settings surface: what it offers, what it refuses, and what it admits.

The interesting assertions here are the ones about honesty rather than
function. A model picker that silently loses to a workspace pin, or a Telegram
panel that says "connected" while a webhook eats every update, is worse than no
panel - it converts a fixable problem into an unexplained one.

Nothing here reaches Ollama or Telegram: `conftest` blocks both, so
`server_running` is False and the payloads degrade exactly as they would on a
machine with nothing running.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from shamsu.session.manager import SessionManager
from shamsu.webui import api
from shamsu.webui.server import WebPortal


@pytest.fixture()
def portal(tmp_path: Path):
    project = tmp_path / "alpha"
    project.mkdir()
    SessionManager(project).create_session("first")
    served = WebPortal(project, port=0)
    served.start()
    yield served
    served.stop()


def call(portal: WebPortal, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        portal.base_url + path, data=data, method="POST" if data is not None else "GET"
    )
    request.add_header("X-Shamsu-Token", portal.token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


# -- settings ------------------------------------------------------------


def test_settings_open_without_touching_a_model_server(portal: WebPortal) -> None:
    """The drawer's one blocking request must not depend on Ollama being up, or
    you cannot open the settings page to find out that Ollama is down."""
    status, payload = call(portal, "/api/settings")
    assert status == 200
    assert set(payload) >= {"model", "context", "verbosity", "tools", "telegram"}


def test_verbosity_round_trips_and_reports_what_it_shows(portal: WebPortal) -> None:
    status, payload = call(portal, "/api/settings", {"verbosity": "verbose"})
    assert status == 200
    assert payload["verbosity"]["level"] == "verbose"
    # Served rather than reimplemented in JS, so all three surfaces filter by
    # the same rule.
    assert "tool.result" in payload["verbosity"]["body_kinds"]

    _, quiet = call(portal, "/api/settings", {"verbosity": "quiet"})
    assert quiet["verbosity"]["body_kinds"] == ["tool.call"]


def test_an_unknown_verbosity_is_refused(portal: WebPortal) -> None:
    status, payload = call(portal, "/api/settings", {"verbosity": "loud"})
    assert status == 400
    assert "quiet" in payload["error"]


def test_a_model_choice_is_stored_install_wide(portal: WebPortal) -> None:
    status, payload = call(portal, "/api/settings", {"model": "qwen3:8b"})
    assert status == 200
    assert payload["model"] == "qwen3:8b"
    assert payload["model_source"] == "install"

    from shamsu.runtime.models import model_for_role

    assert model_for_role("agent-chat") == "qwen3:8b"


def test_a_model_outside_the_cookbook_is_allowed(portal: WebPortal) -> None:
    """Anything pulled is offerable. The capability flags fall back to family
    names for unknown models, so refusing them would be a restriction with
    nothing behind it."""
    status, payload = call(portal, "/api/settings", {"model": "deepseek-r1:14b"})
    assert status == 200
    assert payload["model"] == "deepseek-r1:14b"


def test_a_model_name_with_spaces_is_refused(portal: WebPortal) -> None:
    status, _ = call(portal, "/api/settings", {"model": "two names"})
    assert status == 400


def test_resetting_the_model_returns_to_the_tier_default(portal: WebPortal) -> None:
    call(portal, "/api/settings", {"model": "qwen3:8b"})
    _, payload = call(portal, "/api/settings", {"model": None})
    assert payload["model_source"] == "tier"


def test_the_environment_still_wins_over_the_browser(portal: WebPortal, monkeypatch) -> None:
    """An operator who exported SHAMSU_MODEL did it for a reason and must not be
    overridden by something clicked in a browser last week."""
    call(portal, "/api/settings", {"model": "qwen3:8b"})
    monkeypatch.setenv("SHAMSU_MODEL", "gemma3:12b")
    _, payload = call(portal, "/api/settings", {"verbosity": "normal"})
    assert payload["model"] == "gemma3:12b"
    assert payload["model_source"] == "env"


# -- models --------------------------------------------------------------


def test_models_route_degrades_when_ollama_is_unreachable(portal: WebPortal) -> None:
    status, payload = call(portal, "/api/models")
    assert status == 200
    assert payload["server_running"] is False
    assert payload["loaded"] == []
    # Still offers the cookbook, so the picker is usable enough to prepare a
    # choice before starting the server.
    assert payload["models"]


def test_a_workspace_pin_is_reported_as_shadowing_the_install_choice(
    portal: WebPortal, monkeypatch
) -> None:
    """The failure this field exists to prevent: you pick a model, a workspace
    pin outranks it, and the page appears to have done nothing."""
    call(portal, "/api/settings", {"model": "qwen3:8b"})
    monkeypatch.setattr("shamsu.runtime.models._ACTIVE_MODEL_OVERRIDE", "qwen2.5:3b-instruct")

    _, payload = call(portal, "/api/models")
    assert payload["source"] == "workspace"
    assert payload["workspace_pin_shadows"] is True
    assert payload["effective"] == "qwen2.5:3b-instruct"


# -- telegram ------------------------------------------------------------


def test_telegram_panel_states_the_transport_rather_than_leaving_it_open(
    portal: WebPortal,
) -> None:
    """SHAMSU only ever long-polls. Leaving that unsaid is how a registered
    webhook went unnoticed."""
    status, payload = call(portal, "/api/telegram")
    assert status == 200
    assert payload["telegram"]["transport"] == "long polling"
    assert payload["telegram"]["running"] is False


def test_starting_without_a_token_is_refused_clearly(portal: WebPortal) -> None:
    status, payload = call(portal, "/api/telegram/start", {})
    assert status == 200
    assert payload["outcome"] == "no-token"
    assert payload["telegram"]["running"] is False


def test_a_junk_token_is_refused_before_it_is_written(portal: WebPortal) -> None:
    status, _ = call(portal, "/api/telegram", {"token": "not-a-token"})
    assert status == 400


def test_pairings_start_empty_and_a_code_can_be_minted(portal: WebPortal) -> None:
    status, payload = call(portal, "/api/telegram/pairings")
    assert status == 200
    assert payload["pairings"] == []

    status, minted = call(portal, "/api/telegram/pairings", {})
    assert status == 201
    assert len(minted["pairing"]["code"]) == 6
    assert minted["pairing"]["expires_at"]


def test_unpairing_an_unknown_device_is_not_an_error(portal: WebPortal) -> None:
    """Idempotent on purpose: two surfaces can be looking at the same list, and
    the second Unpair press must not produce a scary failure."""
    status, payload = call(portal, "/api/telegram/pairings/4242/unpair", {})
    assert status == 200
    assert payload["pairings"] == []


def test_binding_to_an_unknown_workspace_is_a_404(portal: WebPortal) -> None:
    status, _ = call(portal, "/api/telegram/bind", {"workspace_id": "deadbeefcafe"})
    assert status == 404


def test_binding_records_the_choice_for_every_surface(portal: WebPortal, tmp_path: Path) -> None:
    other = tmp_path / "beta"
    other.mkdir()
    call(portal, "/api/workspaces", {"path": str(other)})

    status, _ = call(
        portal, "/api/telegram/bind", {"workspace_id": api.workspace_id(other)}
    )
    assert status == 200

    from shamsu.runtime.settings import telegram_workspace

    assert telegram_workspace() == other.resolve()


# -- locks ---------------------------------------------------------------


def test_locks_are_listed_and_stale_ones_can_be_released(portal: WebPortal) -> None:
    status, payload = call(portal, "/api/locks")
    assert status == 200
    assert payload["leases"] == []
    assert payload["machine_slot_held"] is False

    status, cleared = call(portal, "/api/locks/clear-stale", {})
    assert status == 200
    assert cleared["released"] == 0


def test_a_live_lock_is_listed_but_not_released(portal: WebPortal) -> None:
    """Being un-stealable is the whole point of a lease. `clear-stale` removes
    only what a dead process left behind."""
    from shamsu.control.store import MACHINE_LEASE_KEY

    store = portal.runner.store
    store.acquire_lease(MACHINE_LEASE_KEY, MACHINE_LEASE_KEY, surface="cli")

    _, payload = call(portal, "/api/locks")
    assert payload["machine_slot_held"] is True

    _, cleared = call(portal, "/api/locks/clear-stale", {})
    assert cleared["released"] == 0
    assert cleared["machine_slot_held"] is True


# -- ollama --------------------------------------------------------------


def test_unload_reports_nothing_when_the_server_is_unreachable(portal: WebPortal) -> None:
    status, payload = call(portal, "/api/ollama/unload", {})
    assert status == 200
    assert payload["unloaded"] == []


def test_unload_frees_the_configured_model_even_outside_the_cookbook(monkeypatch) -> None:
    """The cookbook was the only way to choose a model when
    `unload_shamsu_models` was written. Now that any pulled model can be
    selected, a button that leaves the one you are running loaded is worse than
    no button."""
    freed: list[str] = []
    monkeypatch.setattr("shamsu.runtime.ollama.unload_shamsu_models", lambda *a, **k: [])
    monkeypatch.setattr(
        "shamsu.runtime.ollama.list_loaded_models", lambda *a, **k: ["mystery-model:8b"]
    )
    monkeypatch.setattr(
        "shamsu.runtime.ollama.unload_model",
        lambda name, *a, **k: (freed.append(name), True)[1],
    )
    monkeypatch.setattr("shamsu.runtime.models.model_for_role", lambda _role: "mystery-model:8b")

    assert api.unload_our_models() == ["mystery-model:8b"]
    assert freed == ["mystery-model:8b"]


def test_unload_leaves_somebody_elses_model_alone(monkeypatch) -> None:
    """Narrow on purpose: an unrelated model another program loaded is not ours
    to evict."""
    monkeypatch.setattr("shamsu.runtime.ollama.unload_shamsu_models", lambda *a, **k: [])
    monkeypatch.setattr(
        "shamsu.runtime.ollama.list_loaded_models", lambda *a, **k: ["someone-elses:70b"]
    )
    monkeypatch.setattr("shamsu.runtime.models.model_for_role", lambda _role: "ours:8b")

    assert api.unload_our_models() == []


# -- routing -------------------------------------------------------------


def test_an_unknown_telegram_action_is_a_404_not_a_500(portal: WebPortal) -> None:
    status, _ = call(portal, "/api/telegram/explode", {})
    assert status == 404
