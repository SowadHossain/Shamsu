"""A `ModelClient` backed by a local Ollama server.

This is the first client that talks to a real model. Everything else in the
runtime has been exercised against `ScriptedModel`, which always emits valid
JSON; this is where the bar set by §19 meets a model that may not clear it.

Four decisions worth stating, because each was made against a measured
alternative rather than a guess:

**1. Requests stream, so cancellation reaches an in-flight generation.**
A 14B model asked for 800 tokens generates for the better part of a minute. A
non-streaming request cannot be abandoned during that window, and "the run
controller can stop a run promptly" is the single defect that most motivates
v2. The consume task is raced against `cancel.wait_cancelled()` exactly as
`tools/testing.py:_spawn` races a subprocess; cancelling it closes the
response, which drops the socket and stops the server generating.

**2. Decoding is constrained to the contract where the server allows it, and
the server is asked rather than predicted.**
Measured against qwen2.5-coder:14b, prose schema hints are not enough. Asked
for an `ImplementationPlan` with `schema_hint` in the prompt, it answered:

    {"summary": "...", "steps": [{"description": "Identify the ..."}]}

fluent, plausible, and nothing like `PlanStepProposal`. Three attempts at
temperature 0 produced the same rejection three times, because temperature 0
makes retrying a *deterministic* re-run of the failure. Constrained decoding
produced valid steps on the first attempt.

Getting `InvestigationStep` to convert took finding out why it did not.
Bisecting its schema one keyword at a time isolated `maxLength: 4000` on
`conclusion`: Ollama expands a length bound into repetition rules, and at 4000
the grammar fails to compile. `ImplementationPlan`, whose longest bound is 300,
was never affected. So `grammar_schema` drops length bounds before offering a
schema -- a *decoding* constraint the contract re-enforces on the way back
anyway. With that, the author loop's contract constrains, and the model
answers with a real `call_tool`.

Which schemas convert still is not predictable in general, so the result is
also *learned*: a grammar rejection is recorded for that contract and every
later call for it uses plain JSON mode. One wasted request per contract per
process, and an Ollama version that converts more is picked up without a code
change.

`ModelRequest.output_schema` still wins outright when set: an explicit schema
is the caller's decision, and it is neither pruned nor second-guessed.

**3. Nothing here repairs output.** `generate_typed` routes through
`parse_json_response` and validates. A response that does not satisfy its
contract raises `ModelContractError`, which the runtime already handles as a
bounded retry. That is the §15 rule and this client does not get an exception
to it -- note in particular that `content` frequently arrives fence-wrapped
even in JSON mode, which is *unwrapping*, not repair, and `normalise` already
does it.

**4. `num_ctx` is sent explicitly, and it matches what `context_tokens`
advertises.** Ollama does not default a model's context window to its maximum;
it applies its own, much smaller default and silently discards the overflow.
Advertising 32k to the context compiler while the server quietly truncates at
4k would produce exactly the class of invisible failure this project exists to
prevent, so the advertised budget and the requested window are the same number
by construction.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from shamsu.interfaces.cancellation import CancellationToken, Cancelled
from shamsu.interfaces.models import (
    ModelContractError,
    ModelRequest,
    ModelResponse,
    ModelTimeout,
    ModelUnavailable,
    ModelUsage,
)
from shamsu.models.normalization import parse_json_response

ContractT = TypeVar("ContractT", bound=BaseModel)

#: Where a stock Ollama listens.
DEFAULT_HOST = "http://localhost:11434"

#: Used when the server cannot be asked what the model's window is. Small
#: enough to be safe on any model rather than optimistic -- an over-estimate
#: gets silently truncated by the server, which is the failure mode with no
#: symptom.
FALLBACK_CONTEXT_TOKENS = 8192

#: Ceiling applied to a probed window. A model's maximum is not free: `num_ctx`
#: allocates a KV cache up front, and a window a machine cannot hold does not
#: fail — it spills into system RAM and runs slowly enough to look broken.
#:
#: 16384 is chosen against the same 8 GB budget as `cli.DEFAULT_OLLAMA_MODEL`.
#: For a 7B with 28 layers and 4 KV heads at head_dim 128, fp16 KV costs about
#: 56 KiB per token, so this window is ~0.9 GB on top of ~4.7 GB of Q4 weights.
#: The model's full 32k would roughly double the cache and leave nothing for
#: compute buffers.
#:
#: Callers with more headroom can raise it; callers who need the whole window
#: can set `context_tokens` exactly.
DEFAULT_MAX_CONTEXT_TOKENS = 16384

#: Long, because a cold model has to be loaded into VRAM before the first token
#: appears. This bounds a *stall*, not the generation: streaming resets it on
#: every chunk received.
DEFAULT_TIMEOUT_SECONDS = 600.0


class OllamaClient:
    """A local model served by Ollama.

    Construction never contacts the server. The context window is probed
    lazily and any failure falls back rather than raising, so building a client
    is safe when Ollama is down -- the honest error belongs on `generate`,
    where it can say the server is unreachable rather than failing during
    argument parsing.
    """

    def __init__(
        self,
        model: str,
        *,
        host: str = DEFAULT_HOST,
        context_tokens: int | None = None,
        max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        structured_output: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._host = host.rstrip("/")
        self._configured_context = context_tokens
        self._max_context = max_context_tokens
        self._timeout = timeout_seconds
        self._structured_output = structured_output
        self._transport = transport
        self._probed_context: int | None = None
        # Contracts this server could not build a grammar for. Learned from
        # its own rejections, so a new Ollama version that supports more is
        # picked up without a code change -- and an older one that supports
        # less does not need one either.
        self._ungrammatical: set[str] = set()

    @property
    def name(self) -> str:
        return self._model

    @property
    def context_tokens(self) -> int:
        """The window the compiler budgets against, and the one actually used.

        Probed once from `/api/show` and cached. The same number is sent as
        `num_ctx` on every request -- see the module docstring on why those two
        must not diverge.
        """
        if self._configured_context is not None:
            return self._configured_context
        if self._probed_context is None:
            self._probed_context = min(self._probe_context_window(), self._max_context)
        return self._probed_context

    def count_tokens(self, text: str) -> int:
        """Roughly four characters per token.

        Deliberately an approximation and deliberately local. The compiler
        calls this while assembling a frame, long before a request exists, so
        it must not need the server; and because the budget it feeds must be
        reproducible, a *stable* approximation beats an accurate one that
        varies with server state. Matches `ScriptedModel.count_tokens`, so a
        frame budgets identically under both clients.
        """
        return max(1, (len(text) + 3) // 4)

    async def generate(self, request: ModelRequest, cancel: CancellationToken) -> ModelResponse:
        cancel.raise_if_cancelled()
        body = await self._chat(request, cancel, force_json=False)
        return _response_from(body)

    async def generate_typed(
        self,
        request: ModelRequest,
        contract: type[ContractT],
        cancel: CancellationToken,
    ) -> ContractT:
        cancel.raise_if_cancelled()
        body = await self._chat_typed(request, contract, cancel)
        response = _response_from(body)

        # A thinking model can spend its entire output budget reasoning and
        # emit no answer at all. That is a contract failure, but the reasoning
        # is what a failure capsule needs to show, so it is carried as the raw
        # text rather than discarded.
        if not response.text.strip():
            thinking = _thinking_of(body)
            raise ModelContractError(
                "the response contained no content"
                + (" (the model produced only reasoning)" if thinking else ""),
                raw_text=thinking,
            )

        payload, reason = parse_json_response(response.text)
        if payload is None:
            raise ModelContractError(reason, raw_text=response.text)

        try:
            return contract.model_validate(payload)
        except ValidationError as exc:
            raise ModelContractError(
                f"response did not satisfy {contract.__name__}: {exc}",
                raw_text=response.text,
            ) from exc

    async def _chat_typed(
        self,
        request: ModelRequest,
        contract: type[ContractT],
        cancel: CancellationToken,
    ) -> Mapping[str, Any]:
        """Constrain decoding to the contract where the server can, else JSON mode.

        Which schemas Ollama can turn into a grammar is not knowable from here:
        `ImplementationPlan` converts, `InvestigationStep` does not, and none of
        its individual constructs is rejected on its own. So the backend is
        asked rather than predicted -- and asked once. A grammar rejection is
        remembered per contract, so the cost is a single wasted request per
        contract per process, not per call.

        This is worth the trouble because the alternative measured badly: with
        prose hints alone, qwen2.5-coder:14b answered `ImplementationPlan` with
        `steps: [{description: ...}]` -- fluent, plausible, and nothing like
        the contract. Constrained decoding produced valid steps first try.
        """
        if request.output_schema is not None or not self._structured_output:
            # An explicit schema is the caller's decision, including the risk
            # that it does not convert; no second guess on their behalf.
            return await self._chat(request, cancel, force_json=True)

        if contract.__name__ not in self._ungrammatical:
            try:
                return await self._chat(
                    request,
                    cancel,
                    force_json=True,
                    schema=grammar_schema(contract.model_json_schema()),
                )
            except _GrammarUnsupported:
                self._ungrammatical.add(contract.__name__)

        return await self._chat(request, cancel, force_json=True)

    async def _chat(
        self,
        request: ModelRequest,
        cancel: CancellationToken,
        *,
        force_json: bool,
        schema: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """POST /api/chat, streamed, abandoning the request on cancellation."""
        payload = self._payload(request, force_json=force_json, schema=schema)

        consume = asyncio.ensure_future(self._consume(payload))
        watcher = asyncio.ensure_future(cancel.wait_cancelled())

        done, _ = await asyncio.wait({consume, watcher}, return_when=asyncio.FIRST_COMPLETED)

        if consume in done:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            return consume.result()

        # Cancelled. Cancelling the consume task unwinds the `stream` context
        # manager, which closes the connection and stops the server generating.
        consume.cancel()
        await asyncio.gather(consume, return_exceptions=True)
        raise Cancelled(cancel.reason or "run cancelled")

    def _payload(
        self,
        request: ModelRequest,
        *,
        force_json: bool,
        schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "temperature": request.temperature,
            "num_predict": request.max_output_tokens,
            "num_ctx": self.context_tokens,
        }
        if request.stop:
            options["stop"] = list(request.stop)

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": True,
            "options": options,
        }

        # Precedence: what the caller asked for, then what the contract allows,
        # then plain JSON mode, which never fails to convert.
        if request.output_schema is not None:
            payload["format"] = dict(request.output_schema)
        elif schema is not None:
            payload["format"] = dict(schema)
        elif force_json:
            payload["format"] = "json"

        return payload

    async def _consume(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Stream one chat response, accumulating it into a single body.

        Ollama emits one JSON object per line, the last carrying `done: true`
        and the token counts. Content is concatenated; the final object
        supplies the accounting.
        """
        content: list[str] = []
        thinking: list[str] = []
        final: dict[str, Any] = {}

        try:
            async with (
                httpx.AsyncClient(
                    base_url=self._host,
                    timeout=self._timeout,
                    transport=self._transport,
                ) as client,
                client.stream("POST", "/api/chat", json=payload) as response,
            ):
                if response.status_code != httpx.codes.OK:
                    raw = await response.aread()
                    raise _http_error(self._model, response.status_code, raw, self._host)

                async for line in response.aiter_lines():
                    chunk = _decode_line(line)
                    if chunk is None:
                        continue

                    message = chunk.get("message")
                    if isinstance(message, Mapping):
                        content.append(_text_field(message, "content"))
                        thinking.append(_text_field(message, "thinking"))

                    if chunk.get("done"):
                        final = dict(chunk)

        except httpx.TimeoutException as exc:
            raise ModelTimeout(f"{self._model} did not respond within {self._timeout:g}s") from exc
        except httpx.HTTPError as exc:
            # Connection refused, DNS failure, a dropped socket: all of them
            # mean the server is not usable, which the runtime must report as
            # an environment problem rather than retry as a flaky response.
            raise ModelUnavailable(f"could not reach Ollama at {self._host}: {exc}") from exc

        final["message"] = {
            "role": "assistant",
            "content": "".join(content),
            "thinking": "".join(thinking),
        }
        return final

    def _probe_context_window(self) -> int:
        """Ask `/api/show` how large this model's window is.

        Synchronous and best-effort: it backs a property, and a property that
        raises when a server is down would make budgeting fail before any
        request was attempted. On any failure the caller gets the fallback.

        The injected transport is honoured here too. It is typed for the async
        client, so this asks whether it also serves sync requests --
        `httpx.MockTransport` does. Without that check a test double would be
        bypassed and the probe would reach a real server, which is exactly what
        `tests/conftest.py` forbids.
        """
        transport = self._transport if isinstance(self._transport, httpx.BaseTransport) else None
        if self._transport is not None and transport is None:
            return FALLBACK_CONTEXT_TOKENS

        try:
            with httpx.Client(transport=transport, timeout=10.0) as client:
                response = client.post(
                    f"{self._host}/api/show",
                    json={"model": self._model},
                )
            if response.status_code != httpx.codes.OK:
                return FALLBACK_CONTEXT_TOKENS
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return FALLBACK_CONTEXT_TOKENS

        if not isinstance(body, Mapping):
            return FALLBACK_CONTEXT_TOKENS

        details = body.get("details")
        if isinstance(details, Mapping):
            declared = details.get("context_length")
            if isinstance(declared, int) and declared > 0:
                return declared

        # Otherwise it is under an architecture-prefixed key, e.g.
        # "qwen2.context_length" -- the prefix varies by model family.
        info = body.get("model_info")
        if isinstance(info, Mapping):
            for key, value in info.items():
                if str(key).endswith(".context_length") and isinstance(value, int) and value > 0:
                    return value

        return FALLBACK_CONTEXT_TOKENS


def list_models(
    host: str = DEFAULT_HOST,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, ...]:
    """Model names this server has pulled, or `()` if it cannot be asked.

    Best-effort by design: this exists so an interface can *offer* choices, and
    an interface that raises because a server is briefly down is worse than one
    that shows an empty list and lets the user type a name anyway.
    """
    try:
        with httpx.Client(transport=transport, timeout=10.0) as client:
            response = client.get(f"{host.rstrip('/')}/api/tags")
        if response.status_code != httpx.codes.OK:
            return ()
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return ()

    if not isinstance(body, Mapping):
        return ()
    models = body.get("models")
    if not isinstance(models, list):
        return ()

    names = [m["name"] for m in models if isinstance(m, Mapping) and isinstance(m.get("name"), str)]
    return tuple(sorted(names))


#: Keywords removed before a schema is offered as a sampling grammar.
#: Ollama expands a length bound into repetition rules, and
#: `InvestigationStep.conclusion` is capped at 4000 characters -- large enough
#: that the generated grammar fails to compile:
#:
#:     HTTP 400 "Failed to initialize samplers: failed to parse grammar"
#:
#: which is why that contract was unconstrainable while `ImplementationPlan`,
#: whose longest bound is 300, was fine. Bisecting the schema one keyword at a
#: time is what found it.
_UNGRAMMATICAL_KEYWORDS = frozenset({"minLength", "maxLength"})


def grammar_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """A contract's schema, reduced to what Ollama can compile into a grammar.

    This drops a *decoding* constraint, never a *validation* one. The contract
    still enforces every bound when the response is parsed, so a model that
    overruns `maxLength` is rejected exactly as before -- the only change is
    that it was not prevented from overrunning mid-generation. Removing a
    constraint the server cannot honour anyway costs nothing and buys
    constrained decoding for the contract that needed it most.

    Note the direction: this reshapes the *request*. Nothing here touches a
    response, which is the line `normalization` draws and this module keeps.
    """

    def prune(node: Any) -> Any:
        if isinstance(node, Mapping):
            return {k: prune(v) for k, v in node.items() if k not in _UNGRAMMATICAL_KEYWORDS}
        if isinstance(node, list):
            return [prune(item) for item in node]
        return node

    pruned = prune(schema)
    return pruned if isinstance(pruned, dict) else dict(schema)


def _decode_line(line: str) -> Mapping[str, Any] | None:
    """Parse one streamed line, ignoring keep-alive blanks."""
    text = line.strip()
    if not text:
        return None
    try:
        decoded = json.loads(text)
    except ValueError:
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _text_field(message: Mapping[str, Any], key: str) -> str:
    value = message.get(key)
    return value if isinstance(value, str) else ""


def _thinking_of(body: Mapping[str, Any]) -> str:
    message = body.get("message")
    return _text_field(message, "thinking") if isinstance(message, Mapping) else ""


def _response_from(body: Mapping[str, Any]) -> ModelResponse:
    message = body.get("message")
    text = _text_field(message, "content") if isinstance(message, Mapping) else ""

    prompt_tokens = body.get("prompt_eval_count")
    output_tokens = body.get("eval_count")
    total_ns = body.get("total_duration")

    return ModelResponse(
        text=text,
        usage=ModelUsage(
            prompt_tokens=prompt_tokens if isinstance(prompt_tokens, int) else 0,
            output_tokens=output_tokens if isinstance(output_tokens, int) else 0,
        ),
        # Ollama reports why it stopped. "length" means the output budget ran
        # out mid-answer, which the caller must be able to distinguish from a
        # model that simply finished.
        truncated=body.get("done_reason") == "length",
        duration_seconds=(total_ns / 1_000_000_000) if isinstance(total_ns, int) else 0.0,
    )


def _http_error(model: str, status: int, raw: bytes, host: str = DEFAULT_HOST) -> Exception:
    detail = raw.decode("utf-8", "replace").strip()
    if status == httpx.codes.NOT_FOUND:
        # Say what *is* there. "Pull it first" is right but unhelpful when the
        # real situation is a typo, or a model that was removed since the
        # session started — both of which are answered by the list.
        pulled = list_models(host)
        available = f" Available: {', '.join(pulled)}." if pulled else ""
        return ModelUnavailable(
            f"Ollama has no model {model!r}.{available} Pull it with: ollama pull {model}"
        )
    message = f"Ollama returned HTTP {status}: {detail[:400]}"
    # The server could not compile the supplied schema into a sampling grammar.
    # Distinguishable so the negotiation can catch it, but still a
    # ModelUnavailable, so that any path which does *not* have a fallback --
    # a caller-supplied schema, or plain `generate` -- reports it plainly
    # rather than letting an internal type escape.
    if status == httpx.codes.BAD_REQUEST and "grammar" in detail.lower():
        return _GrammarUnsupported(message)
    return ModelUnavailable(message)


class _GrammarUnsupported(ModelUnavailable):
    """This server cannot constrain decoding to the schema it was given.

    Caught by `generate_typed` when the schema was *its* idea: the contract is
    recorded as unconstrainable and the call retried in plain JSON mode. When
    the schema came from the caller there is no fallback to make on their
    behalf, so it propagates as the `ModelUnavailable` it also is.
    """


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_MAX_CONTEXT_TOKENS",
    "DEFAULT_TIMEOUT_SECONDS",
    "FALLBACK_CONTEXT_TOKENS",
    "OllamaClient",
    "grammar_schema",
    "list_models",
]
