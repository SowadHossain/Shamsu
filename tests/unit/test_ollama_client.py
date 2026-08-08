"""The Ollama client, exercised without a live model.

Per `tests/conftest.py` this suite never contacts a real server: every request
here is served by an `httpx` transport double. What that can prove is the
integration contract -- how a streamed response is assembled, which failures
map to which exception, that cancellation actually reaches the request, and
that the advertised context window is the one sent to the server. What it
cannot prove is whether a 14B model emits valid JSON, which is a question only
the GPU machine can answer.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from shamsu.interfaces.cancellation import Cancelled, NullCancellationToken
from shamsu.interfaces.models import (
    ModelClient,
    ModelContractError,
    ModelMessage,
    ModelRequest,
    ModelTimeout,
    ModelUnavailable,
)
from shamsu.models.contracts import InvestigationStep
from shamsu.models.ollama import FALLBACK_CONTEXT_TOKENS, OllamaClient, grammar_schema

NULL = NullCancellationToken()


def _request(**kwargs: Any) -> ModelRequest:
    return ModelRequest(
        messages=(ModelMessage(role="user", content="fix the adder"),),
        max_output_tokens=256,
        **kwargs,
    )


def _stream(*chunks: dict[str, Any]) -> bytes:
    """Ollama's wire format: one JSON object per line."""
    return "\n".join(json.dumps(chunk) for chunk in chunks).encode()


def _say(text: str, *, thinking: str = "", **final: Any) -> bytes:
    """A complete response delivered as two chunks, as a real stream is."""
    return _stream(
        {"message": {"role": "assistant", "content": text, "thinking": thinking}},
        {"message": {"role": "assistant", "content": ""}, "done": True, **final},
    )


class _Recorder:
    """Serves a fixed body and keeps the request that asked for it."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"details": {"context_length": 32768}})
        self.payloads.append(json.loads(request.content))
        return httpx.Response(self.status, content=self.body)

    @property
    def payload(self) -> dict[str, Any]:
        return self.payloads[-1]


def _client(handler: Any, **kwargs: Any) -> OllamaClient:
    return OllamaClient(
        "test-model",
        transport=httpx.MockTransport(handler),
        context_tokens=kwargs.pop("context_tokens", 4096),
        **kwargs,
    )


class TestProtocolConformance:
    def test_the_client_satisfies_the_model_client_protocol(self) -> None:
        assert isinstance(_client(_Recorder(_say("{}"))), ModelClient)

    def test_counting_tokens_needs_no_server(self) -> None:
        """The compiler budgets a frame long before a request exists.

        The transport raises, so any network access fails the test rather than
        being quietly tolerated.
        """

        def explode(request: httpx.Request) -> httpx.Response:
            raise AssertionError("count_tokens must not contact the server")

        client = _client(explode)
        assert client.count_tokens("abcd") == 1
        assert client.count_tokens("") == 1
        assert client.count_tokens("a" * 400) == 100

    def test_construction_contacts_nothing(self) -> None:
        """An unreachable server must not break argument parsing."""

        def explode(request: httpx.Request) -> httpx.Response:
            raise AssertionError("construction must not contact the server")

        OllamaClient("test-model", transport=httpx.MockTransport(explode))


class TestStreamAssembly:
    def test_chunks_are_concatenated_into_one_response(self) -> None:
        handler = _Recorder(
            _stream(
                {"message": {"content": '{"acti'}},
                {"message": {"content": 'on": "conclude"}'}},
                {"message": {"content": ""}, "done": True, "done_reason": "stop"},
            )
        )
        result = asyncio.run(_client(handler).generate(_request(), NULL))
        assert result.text == '{"action": "conclude"}'

    def test_usage_and_duration_come_from_the_final_chunk(self) -> None:
        handler = _Recorder(
            _say(
                "hello",
                done_reason="stop",
                prompt_eval_count=39,
                eval_count=237,
                total_duration=2_500_000_000,
            )
        )
        result = asyncio.run(_client(handler).generate(_request(), NULL))
        assert result.usage.prompt_tokens == 39
        assert result.usage.output_tokens == 237
        assert result.duration_seconds == pytest.approx(2.5)

    def test_hitting_the_output_limit_is_reported_as_truncated(self) -> None:
        """A model that ran out of budget must be distinguishable from one that finished."""
        handler = _Recorder(_say("part", done_reason="length"))
        assert asyncio.run(_client(handler).generate(_request(), NULL)).truncated is True

    def test_a_natural_stop_is_not_truncated(self) -> None:
        handler = _Recorder(_say("done", done_reason="stop"))
        assert asyncio.run(_client(handler).generate(_request(), NULL)).truncated is False

    def test_blank_keepalive_lines_are_ignored(self) -> None:
        handler = _Recorder(b'\n\n{"message": {"content": "hi"}, "done": true}\n\n')
        assert asyncio.run(_client(handler).generate(_request(), NULL)).text == "hi"


class TestTheRequestSent:
    def test_the_advertised_window_is_the_one_requested(self) -> None:
        """num_ctx must equal context_tokens.

        Ollama does not default to a model's maximum window; it applies a
        smaller one and silently drops the overflow. If the compiler budgets
        against a number the server does not honour, prompts are truncated with
        no symptom at all -- so these two are asserted equal.
        """
        handler = _Recorder(_say("{}"))
        client = _client(handler, context_tokens=16384)
        asyncio.run(client.generate(_request(), NULL))
        assert handler.payload["options"]["num_ctx"] == 16384 == client.context_tokens

    def test_an_unconfigured_window_is_probed_from_the_server(self) -> None:
        """/api/show reports 32768, and it is honoured when the cap allows."""
        handler = _Recorder(_say("{}"))
        client = OllamaClient(
            "test-model",
            transport=httpx.MockTransport(handler),
            max_context_tokens=65536,
        )
        assert client.context_tokens == 32768

    def test_the_probed_window_is_capped(self) -> None:
        """A model's maximum is not free -- num_ctx allocates a KV cache."""
        handler = _Recorder(_say("{}"))
        client = OllamaClient(
            "test-model",
            transport=httpx.MockTransport(handler),
            max_context_tokens=8192,
        )
        assert client.context_tokens == 8192

    def test_the_probe_goes_through_the_injected_transport(self) -> None:
        """Otherwise the probe reaches a real server, which conftest forbids.

        The double is asked for `/api/show` and answers; a client that used a
        bare `httpx.post` would bypass it entirely and silently contact
        localhost.
        """
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, json={"details": {"context_length": 12288}})

        client = OllamaClient(
            "test-model",
            transport=httpx.MockTransport(handler),
            max_context_tokens=65536,
        )
        assert client.context_tokens == 12288
        assert seen == ["/api/show"]

    def test_an_unreachable_server_falls_back_rather_than_raising(self) -> None:
        """Budgeting must not fail before a request has even been attempted."""

        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = OllamaClient("test-model", transport=httpx.MockTransport(refuse))
        assert client.context_tokens == FALLBACK_CONTEXT_TOKENS

    def test_a_probed_window_is_the_one_sent_as_num_ctx(self) -> None:
        handler = _Recorder(_say("{}"))
        client = OllamaClient(
            "test-model",
            transport=httpx.MockTransport(handler),
            max_context_tokens=65536,
        )
        asyncio.run(client.generate(_request(), NULL))
        assert handler.payload["options"]["num_ctx"] == 32768

    def test_generation_settings_are_forwarded(self) -> None:
        handler = _Recorder(_say("{}"))
        request = _request(temperature=0.4, stop=("STOP",))
        asyncio.run(_client(handler).generate(request, NULL))

        options = handler.payload["options"]
        assert options["temperature"] == pytest.approx(0.4)
        assert options["num_predict"] == 256
        assert options["stop"] == ["STOP"]
        assert handler.payload["stream"] is True

    def test_a_typed_call_constrains_decoding_to_the_contract(self) -> None:
        """Prose hints alone are not enough for a 14B model; the grammar is."""
        handler = _Recorder(_say('{"action": "conclude", "conclusion": "done"}'))
        asyncio.run(_client(handler).generate_typed(_request(), InvestigationStep, NULL))
        assert handler.payload["format"] == grammar_schema(InvestigationStep.model_json_schema())

    def test_an_untyped_call_asks_for_nothing(self) -> None:
        """Plain generation may legitimately want prose."""
        handler = _Recorder(_say("some prose"))
        asyncio.run(_client(handler).generate(_request(), NULL))
        assert "format" not in handler.payload

    def test_an_explicit_schema_wins_and_is_not_second_guessed(self) -> None:
        schema = {"type": "object", "properties": {"action": {"type": "string"}}}
        handler = _Recorder(_say('{"action": "conclude", "conclusion": "x"}'))
        asyncio.run(
            _client(handler).generate_typed(_request(output_schema=schema), InvestigationStep, NULL)
        )
        assert handler.payload["format"] == schema

    def test_structured_output_can_be_disabled(self) -> None:
        """A comparison run needs to be able to turn the grammar off."""
        handler = _Recorder(_say('{"action": "conclude", "conclusion": "x"}'))
        client = OllamaClient(
            "test-model",
            transport=httpx.MockTransport(handler),
            context_tokens=4096,
            structured_output=False,
        )
        asyncio.run(client.generate_typed(_request(), InvestigationStep, NULL))
        assert handler.payload["format"] == "json"


class TestGrammarSchema:
    """Length bounds are dropped before a schema is offered as a grammar.

    Ollama expands `maxLength` into repetition rules, and `conclusion`'s cap of
    4000 produced a grammar it could not compile -- which is the whole reason
    `InvestigationStep` was unconstrainable while `ImplementationPlan` was not.
    """

    def test_length_bounds_are_pruned_at_every_depth(self) -> None:
        pruned = grammar_schema(InvestigationStep.model_json_schema())
        rendered = json.dumps(pruned)
        assert "maxLength" not in rendered
        assert "minLength" not in rendered

    def test_nothing_else_is_touched(self) -> None:
        """It removes a decoding constraint, not information."""
        original = InvestigationStep.model_json_schema()
        pruned = grammar_schema(original)
        assert pruned["properties"]["action"]["enum"] == ["call_tool", "conclude"]
        assert pruned["required"] == original["required"]
        assert "$defs" in pruned

    def test_the_contract_still_enforces_the_bound(self) -> None:
        """The bound is re-checked on the way back, so nothing is loosened."""
        with pytest.raises(ValidationError):
            InvestigationStep.model_validate({"action": "conclude", "conclusion": "x" * 4001})


class TestGrammarNegotiation:
    """Which schemas convert is learned from the server, not predicted.

    `ImplementationPlan` converts and `InvestigationStep` does not, on the same
    server, and no individual construct in the latter is rejected on its own --
    so the client offers the schema and believes the answer.
    """

    class _RefusesGrammar:
        """Rejects a schema the way Ollama does, and accepts plain JSON mode."""

        def __init__(self) -> None:
            self.formats: list[Any] = []

        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/show":
                return httpx.Response(200, json={"details": {"context_length": 32768}})
            fmt = json.loads(request.content).get("format")
            self.formats.append(fmt)
            if isinstance(fmt, dict):
                return httpx.Response(
                    400,
                    content=b'{"error":"Failed to initialize samplers: failed to parse grammar"}',
                )
            return httpx.Response(200, content=_say('{"action": "conclude", "conclusion": "ok"}'))

    def test_a_grammar_rejection_falls_back_to_json_mode(self) -> None:
        handler = self._RefusesGrammar()
        step = asyncio.run(_client(handler).generate_typed(_request(), InvestigationStep, NULL))
        assert step.conclusion == "ok"
        assert handler.formats == [grammar_schema(InvestigationStep.model_json_schema()), "json"]

    def test_the_rejection_is_remembered_so_it_costs_one_request_once(self) -> None:
        handler = self._RefusesGrammar()
        client = _client(handler)

        asyncio.run(client.generate_typed(_request(), InvestigationStep, NULL))
        asyncio.run(client.generate_typed(_request(), InvestigationStep, NULL))

        # Offered once; every later call goes straight to JSON mode.
        assert handler.formats == [
            grammar_schema(InvestigationStep.model_json_schema()),
            "json",
            "json",
        ]

    def test_an_explicit_schema_rejection_still_surfaces(self) -> None:
        """The caller asked for that schema; falling back would hide it."""
        handler = self._RefusesGrammar()
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        with pytest.raises(ModelUnavailable, match="grammar"):
            asyncio.run(
                _client(handler).generate_typed(
                    _request(output_schema=schema), InvestigationStep, NULL
                )
            )


class TestContractParsing:
    def test_a_valid_response_becomes_its_contract(self) -> None:
        handler = _Recorder(_say('{"action": "conclude", "conclusion": "the bug is on line 4"}'))
        step = asyncio.run(_client(handler).generate_typed(_request(), InvestigationStep, NULL))
        assert step.action == "conclude"
        assert step.conclusion == "the bug is on line 4"

    def test_a_fenced_response_is_unwrapped_not_repaired(self) -> None:
        """Models emit fences even in JSON mode. Removing wrapping is not editing content."""
        handler = _Recorder(_say('```json\n{"action": "conclude", "conclusion": "ok"}\n```'))
        step = asyncio.run(_client(handler).generate_typed(_request(), InvestigationStep, NULL))
        assert step.conclusion == "ok"

    def test_unparseable_output_raises_and_keeps_the_raw_text(self) -> None:
        handler = _Recorder(_say("I think the answer is probably fine."))
        with pytest.raises(ModelContractError) as caught:
            asyncio.run(_client(handler).generate_typed(_request(), InvestigationStep, NULL))
        assert caught.value.raw_text == "I think the answer is probably fine."

    def test_valid_json_that_misses_the_contract_raises(self) -> None:
        """Parseable is not the same as correct; the contract still decides."""
        handler = _Recorder(_say('{"action": "teleport"}'))
        with pytest.raises(ModelContractError, match="InvestigationStep"):
            asyncio.run(_client(handler).generate_typed(_request(), InvestigationStep, NULL))

    def test_reasoning_only_output_is_a_failure_that_shows_the_reasoning(self) -> None:
        """A thinking model can spend its whole budget reasoning and answer nothing.

        The reasoning is what a failure capsule needs, so it survives as the
        raw text rather than being dropped for being empty.
        """
        handler = _Recorder(_say("", thinking="Let me consider the options at length..."))
        with pytest.raises(ModelContractError, match="only reasoning"):
            asyncio.run(_client(handler).generate_typed(_request(), InvestigationStep, NULL))

    def test_nothing_is_salvaged_from_two_objects(self) -> None:
        """Two candidates is ambiguity; picking one would be a guess."""
        handler = _Recorder(_say('{"action": "conclude", "conclusion": "a"} {"action": "x"}'))
        with pytest.raises(ModelContractError):
            asyncio.run(_client(handler).generate_typed(_request(), InvestigationStep, NULL))


class TestFailureMapping:
    def test_an_unreachable_server_is_unavailable_not_a_timeout(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with pytest.raises(ModelUnavailable, match="could not reach Ollama"):
            asyncio.run(_client(refuse).generate(_request(), NULL))

    def test_a_missing_model_says_how_to_pull_it(self) -> None:
        def missing(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=b'{"error":"model not found"}')

        with pytest.raises(ModelUnavailable, match="ollama pull test-model"):
            asyncio.run(_client(missing).generate(_request(), NULL))

    def test_a_server_error_carries_the_detail(self) -> None:
        def broken(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, content=b"failed to parse grammar")

        with pytest.raises(ModelUnavailable, match="failed to parse grammar"):
            asyncio.run(_client(broken).generate(_request(), NULL))

    def test_a_stall_is_a_timeout(self) -> None:
        def stall(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out")

        with pytest.raises(ModelTimeout, match="did not respond"):
            asyncio.run(_client(stall).generate(_request(), NULL))


class _SlowStream(httpx.AsyncBaseTransport):
    """Streams forever, so cancellation has something to interrupt."""

    def __init__(self) -> None:
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        outer = self

        async def body() -> Any:
            try:
                while True:
                    yield b'{"message": {"content": "tick"}}\n'
                    await asyncio.sleep(0.05)
            finally:
                outer.closed = True

        return httpx.Response(200, content=body())


class TestCancellation:
    def test_cancellation_abandons_an_in_flight_generation(self) -> None:
        """Raced, not polled between calls.

        A 14B model generating for a minute is exactly the window in which a
        stop must take effect; v1 could not interrupt one at all.
        """
        from shamsu.runtime import RunToken

        transport = _SlowStream()
        client = OllamaClient("test-model", transport=transport, context_tokens=4096)

        async def scenario() -> None:
            token = RunToken()
            asyncio.get_running_loop().call_later(0.05, token.request, "user interrupt")
            with pytest.raises(Cancelled, match="user interrupt"):
                await client.generate(_request(), token)

        asyncio.run(scenario())
        assert transport.closed is True, "the response stream must be closed on cancellation"

    def test_an_already_cancelled_token_stops_before_requesting(self) -> None:
        from shamsu.runtime import RunToken

        def explode(request: httpx.Request) -> httpx.Response:
            raise AssertionError("a cancelled run must not send a request")

        token = RunToken()
        token.request("stopped")
        with pytest.raises(Cancelled):
            asyncio.run(_client(explode).generate(_request(), token))
