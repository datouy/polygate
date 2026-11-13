"""Runtime snapshot atomicity regression test."""

from polygate.runtime import RuntimeState


def test_reload_preserves_circuit_state_and_publishes_atomically():
    cfg = {
        "db_path": None,
        "health": {"enabled": False},
        "providers": {"mock": {"base_url": "http://127.0.0.1:9/v1", "api_key": "k"}},
        "models": [{"name": "m", "providers": [{"provider": "mock", "model": "m"}]}],
    }
    state = RuntimeState(cfg)
    old_cb = state.current.circuits["mock"]
    old_cb.record_failure()
    assert old_cb.consecutive_failures == 1

    new_cfg = {
        "db_path": None,
        "health": {"enabled": False},
        "providers": {"mock": {"base_url": "http://127.0.0.1:8/v1", "api_key": "k2"}},
        "models": [{"name": "m", "providers": [{"provider": "mock", "model": "m"}]}],
    }
    state.reload(new_cfg)
    assert state.current.providers["mock"].base_url == "http://127.0.0.1:8/v1"
    assert state.current.circuits["mock"] is old_cb
    assert old_cb.consecutive_failures == 1  # not reset by reload
