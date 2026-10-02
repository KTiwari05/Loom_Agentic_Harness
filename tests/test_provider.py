"""Phase 11 tests — provider abstraction hardening (master plan §5, schema §3).

Model identity is per-assistant-record; transport errors become synthetic assistant
records; usage is the provider-neutral superset with nullable cache fields.
"""

from __future__ import annotations

import time

import loom.models as models
from loom.models import ModelResponse
from loom.server.bridge import SessionRunner
from loom.server.eventstore import EventStore
from loom.server.logsink import normalize_usage


class _NS:
    """Attribute bag standing in for a LiteLLM response object."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


# ── models.complete: actual model, request id, cache usage ───────────────────────


def test_complete_captures_actual_model_request_id_and_cache(monkeypatch):
    fake_resp = _NS(
        model="deepseek-v4-flash-0607",  # provider's answer, not what we asked for
        id="req_abc123",
        choices=[_NS(message=_NS(content="hello", tool_calls=None))],
        usage=_NS(
            prompt_tokens=100,
            completion_tokens=5,
            prompt_tokens_details=_NS(cached_tokens=64),
        ),
    )
    monkeypatch.setattr(models.litellm, "completion", lambda **kw: fake_resp)
    monkeypatch.setattr(models.litellm, "completion_cost", lambda **kw: 0.0)

    resp = models.complete("deepseek/deepseek-v4-flash", [{"role": "user", "content": "hi"}], None)

    assert resp.model == "deepseek-v4-flash-0607"
    assert resp.request_id == "req_abc123"
    assert resp.usage["cache_read_input_tokens"] == 64
    assert "cache_creation_input_tokens" not in resp.usage  # not reported → not invented


def test_normalize_usage_superset():
    bare = normalize_usage({"prompt_tokens": 10, "completion_tokens": 2})
    assert bare == {
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
    }
    cached = normalize_usage(
        {"prompt_tokens": 10, "completion_tokens": 2,
         "cache_read_input_tokens": 8, "cache_creation_input_tokens": 3}
    )
    assert cached["cache_read_input_tokens"] == 8
    assert cached["cache_creation_input_tokens"] == 3


# ── live scripted session: model switch + transport error ────────────────────────


def _final(text: str, model: str, request_id: str = "") -> ModelResponse:
    return ModelResponse(
        text=text, tool_calls=[],
        assistant_message={"role": "assistant", "content": text},
        usage={"prompt_tokens": 4, "completion_tokens": 2},
        cost=0.0, model=model, request_id=request_id,
    )


def _run_and_wait(runner, text):
    runner.send(text)
    deadline = time.time() + 8
    while time.time() < deadline and runner.busy:
        time.sleep(0.05)
    while runner.busy:
        time.sleep(0.05)


def _runner(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    monkeypatch.delenv("LOOM_STOP_CONDITION", raising=False)
    store = EventStore(tmp_path / "store")
    project = tmp_path / "proj"
    project.mkdir()
    meta = store.create(workdir=str(project), model="fake-a")
    return store, meta, SessionRunner(meta["id"], store)


def test_mid_session_model_switch_records_two_model_ids(tmp_path, monkeypatch):
    def fake(model, messages, tools, temperature):
        # echo the requested model back, like a real provider does
        return _final(f"answered by {model}", model=model, request_id=f"req_{model}")

    monkeypatch.setattr(models, "complete", fake)
    store, meta, runner = _runner(tmp_path, monkeypatch)

    _run_and_wait(runner, "first turn")
    result = runner.command("model", "fake-b")
    assert "fake-b" in result["text"]
    _run_and_wait(runner, "second turn")

    records = store.log_for(meta["id"]).read_all()
    assistants = [r for r in records if r.get("type") == "assistant"]
    assert [a["message"]["model"] for a in assistants] == ["fake-a", "fake-b"]
    assert [a["requestId"] for a in assistants] == ["req_fake-a", "req_fake-b"]
    # identical behavior either side of the switch: both turns completed cleanly
    assert [a["message"]["stop_reason"] for a in assistants] == ["end_turn", "end_turn"]
    assert not runner.busy


def test_transport_error_becomes_synthetic_assistant_record(tmp_path, monkeypatch):
    def fake(model, messages, tools, temperature):
        raise RuntimeError("Connection reset by provider (status 502)")

    monkeypatch.setattr(models, "complete", fake)
    store, meta, runner = _runner(tmp_path, monkeypatch)
    _run_and_wait(runner, "doomed turn")

    records = store.log_for(meta["id"]).read_all()
    errs = [r for r in records if r.get("type") == "assistant" and r.get("isApiErrorMessage")]
    assert len(errs) == 1
    msg = errs[0]["message"]
    assert msg["model"] == "<synthetic>"
    assert msg["stop_reason"] == "error"
    assert "502" in msg["content"][0]["text"]
    assert msg["usage"]["input_tokens"] == 0 and msg["usage"]["cache_read_input_tokens"] is None
    assert not runner.busy  # the session survives the error and accepts the next turn
