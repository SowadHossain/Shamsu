from __future__ import annotations

import shamsu.runtime.ollama as ollama
import shamsu.runtime.session_registry as registry

# The DEFAULT tier's thinking anchor, read from the cookbook rather than
# hardcoded: swapping the anchor (qwen3:8b -> qwen3.5:9b-q4_K_M, 2026-08-18)
# broke 17 tests that had the old name baked in. The behaviour under test is
# "the anchor is required/allowed/routed to", never "it is called qwen3:8b".
from shamsu.runtime.models import ModelTier, TIER_MODEL_SPECS

ANCHOR = TIER_MODEL_SPECS[ModelTier.DEFAULT][0].name


def _use_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_base_dir", lambda: tmp_path)


def _fake_alive(monkeypatch, alive_pids):
    alive = set(alive_pids)
    monkeypatch.setattr(registry, "pid_alive", lambda pid: pid in alive)
    monkeypatch.setattr(ollama, "pid_alive", lambda pid: pid in alive)


def test_register_and_live_pids_prunes_dead(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    registry.register_session(111)
    registry.register_session(222)
    _fake_alive(monkeypatch, {111})  # 222 has crashed

    live = registry.live_session_pids()
    assert live == [111]
    # Dead session's PID file is pruned on the way.
    assert not (tmp_path / "sessions" / "222.pid").exists()


def test_live_pids_can_exclude_self(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    registry.register_session(111)
    registry.register_session(222)
    _fake_alive(monkeypatch, {111, 222})

    assert registry.live_session_pids(exclude=111) == [222]


def test_ownership_round_trip(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    assert registry.read_ollama_owner() is None
    registry.claim_ollama_ownership(555, started_by=111)
    assert registry.read_ollama_owner()["serve_pid"] == 555
    registry.clear_ollama_ownership()
    assert registry.read_ollama_owner() is None


def test_shutdown_does_nothing_when_another_session_is_live(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    registry.register_session(111)
    registry.register_session(222)
    _fake_alive(monkeypatch, {111, 222})
    calls = {"stop": [], "unload": []}

    result = ollama.shutdown_if_last_session(
        111,
        server_stopper=lambda pid: calls["stop"].append(pid) or True,
        model_unloader=lambda url: calls["unload"].append(url) or ["x"],
    )

    assert result == "not-last"
    assert calls == {"stop": [], "unload": []}
    # This session still deregistered itself.
    assert not (tmp_path / "sessions" / "111.pid").exists()


def test_shutdown_stops_server_when_shamsu_owns_it(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    registry.register_session(111)
    registry.claim_ollama_ownership(555)
    _fake_alive(monkeypatch, {111, 555})
    stopped = []

    result = ollama.shutdown_if_last_session(
        111,
        server_stopper=lambda pid: stopped.append(pid) or True,
        model_unloader=lambda url: (_ for _ in ()).throw(AssertionError("should not unload")),
    )

    assert result == "stopped-server"
    assert stopped == [555]
    assert registry.read_ollama_owner() is None  # marker cleared


def test_shutdown_unloads_models_when_server_not_owned(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    registry.register_session(111)
    _fake_alive(monkeypatch, {111})  # no ownership marker at all
    unloaded_from = []

    result = ollama.shutdown_if_last_session(
        111,
        server_stopper=lambda pid: (_ for _ in ()).throw(AssertionError("should not stop")),
        model_unloader=lambda url: unloaded_from.append(url) or [ANCHOR],
    )

    assert result == "unloaded"
    assert len(unloaded_from) == 1


def test_shutdown_clears_stale_owner_then_unloads(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    registry.register_session(111)
    registry.claim_ollama_ownership(999)  # owner PID already dead
    _fake_alive(monkeypatch, {111})

    result = ollama.shutdown_if_last_session(
        111,
        server_stopper=lambda pid: (_ for _ in ()).throw(AssertionError("dead pid must not be stopped")),
        model_unloader=lambda url: [ANCHOR],
    )

    assert result == "unloaded"
    assert registry.read_ollama_owner() is None  # stale marker cleared


def test_unload_shamsu_models_only_touches_known_models(monkeypatch):
    monkeypatch.setattr(ollama, "list_loaded_models", lambda base_url="x": [ANCHOR, "randomapp:1b"])
    unloaded = []
    monkeypatch.setattr(ollama, "unload_model", lambda name, base_url="x": unloaded.append(name) or True)

    result = ollama.unload_shamsu_models("http://localhost:11434")

    assert result == [ANCHOR]
    assert unloaded == [ANCHOR]  # the unrelated model was left alone


def test_shutdown_never_stops_falkordb_on_last_exit(monkeypatch, tmp_path):
    """FalkorDB used to be auto-stopped here on the last session's exit, which
    meant memory silently went cold between ordinary REPL sessions. It's the
    user's container to stop via `docker stop` - shutdown must never touch it,
    last session or not."""
    _use_tmp(monkeypatch, tmp_path)
    registry.register_session(111)
    _fake_alive(monkeypatch, {111})  # last session standing

    result = ollama.shutdown_if_last_session(
        111,
        server_stopper=lambda pid: True,
        model_unloader=lambda url: [ANCHOR],
    )

    assert result == "unloaded"


def test_shutdown_accepts_no_graphiti_stopper_kwarg(monkeypatch, tmp_path):
    """Regression guard: the removed `graphiti_stopper` parameter must not
    reappear as a required/accepted kwarg by accident."""
    import inspect

    params = inspect.signature(ollama.shutdown_if_last_session).parameters
    assert "graphiti_stopper" not in params
