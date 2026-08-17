"""A lost mutation call must be reported as what it is.

Before this, a turn that emitted a complete, correct write_file call which failed
to parse got the same correction as a turn that never tried: "Do not provide
prose". At temperature 0.1 the model answered by re-emitting the identical broken
call, so 2026-08-03 burned every mutation round and wrote nothing while the
harness, the user, and the telemetry all blamed planning.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.tools.agent_tools import AgentToolRegistry

FIXTURES = Path(__file__).parent / "fixtures"
# The 2026-08-03 payload. As of slice 1.2 this is RECOVERABLE, so it exercises the
# success path, not the failure path.
REPAIRABLE = (FIXTURES / "qwen_unescaped_write_file_2026_08_03.txt").read_text(encoding="utf-8")
# Cut off mid-payload: the braces never close, so there is genuinely nothing to
# recover. This is what drives the honest-failure path below.
# Deliberately longer than the 300-char clip used in the terminal message, so the
# persistence test can prove the WHOLE response is kept rather than the summary.
TRUNCATED = (
    '{"name": "write_file", "arguments": {"filepath": "templates/my_orders.html", '
    '"content": "{% extends \\"base.html\\" %}\\n'
    "{% block content %}\\n<h1>My orders</h1>\\n<table>\\n"
    "  <tr><th>Order</th><th>Item</th><th>Total</th><th>Status</th></tr>\\n"
    "  {% for order in orders %}\\n"
    "  <tr><td>{{ order.id }}</td><td>{{ order.item.title }}</td>\\n"
    "      <td>{{ order.final_amount }}</td><td>{{ order.status }}</td></tr>\\n"
    '  {% empty %}\\n  <tr><td colspan=\\"4\\">No orders yet.</td></tr>\\n'
    "  {% endfor %}\\n</table>\\n    <td>"
)

REQUEST = "Use write_file to create templates/my_orders.html with the orders table."


class RepeatingClient:
    """Returns the same unparseable payload every round, like the real run did."""

    def __init__(self, content: str, rounds: int = 12) -> None:
        self._content = content
        self._rounds = rounds
        self.options_seen: list[dict] = []
        self.messages_seen: list[list[dict]] = []

    async def chat(self, model, messages, stream, options, **kwargs):
        self.options_seen.append(dict(options or {}))
        self.messages_seen.append([dict(m) for m in messages])
        self._rounds -= 1
        if self._rounds < 0:
            return {"message": {"content": "I give up.", "tool_calls": []}}
        return {"message": {"content": self._content, "tool_calls": []}}


def _loop(tmp_path: Path, client, ledger: ActionLedger | None = None) -> AgentChatLoop:
    return AgentChatLoop(
        tmp_path,
        client=client,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _request: True),
        model_name="qwen2.5-coder:7b-instruct",
        action_ledger=ledger,
    )


def _user_messages(messages: list[dict]) -> list[str]:
    return [str(m.get("content", "")) for m in messages if m.get("role") == "user"]


@pytest.mark.asyncio
async def test_an_unparseable_write_call_is_not_reported_as_prose(tmp_path: Path):
    client = RepeatingClient(TRUNCATED)
    loop = _loop(tmp_path, client)

    await loop.run(REQUEST)

    corrections = [
        text
        for messages in client.messages_seen
        for text in _user_messages(messages)
        if "could not parse" in text
    ]
    assert corrections, "no honest correction was ever sent"
    correction = corrections[0]
    # It must name the real cause and never call it prose.
    assert "encoding failure, not a planning failure" in correction
    assert "cut off before the JSON closed" in correction
    # And it must show the raw form for the path the model was actually targeting.
    assert "# write_file: templates/my_orders.html" in correction
    assert "Do not provide prose" not in correction


@pytest.mark.asyncio
async def test_an_identical_repeat_escalates_sampling(tmp_path: Path):
    """At temperature 0.1 a retry CANNOT differ, so re-nagging is a wasted round."""
    client = RepeatingClient(TRUNCATED)
    loop = _loop(tmp_path, client)

    await loop.run(REQUEST)

    temperatures = [o.get("temperature") for o in client.options_seen]
    assert temperatures[0] == 0.1
    assert any(t is not None and t > 0.1 for t in temperatures), temperatures
    assert any("seed" in o for o in client.options_seen)


@pytest.mark.asyncio
async def test_a_persistent_repeat_evicts_the_failed_attempt_from_the_prompt(tmp_path: Path):
    """The eviction happens in the chat state, which is where the bloat lived.

    This used to assert on the outgoing prompt. Prompts are now compiled from
    runtime state rather than replayed conversation, so no assistant turn
    reaches the model at all - asserting on the wire would pass for the wrong
    reason (nothing is there) and stop protecting anything. The invariant that
    matters is unchanged: a repeated unparseable attempt must not accumulate.
    """
    client = RepeatingClient(TRUNCATED)
    loop = _loop(tmp_path, client)

    await loop.run(REQUEST)

    assistant = [
        message.content
        for message in loop.state.all_messages
        if message.role == "assistant"
    ]
    assert any("unparseable tool call omitted" in text for text in assistant)
    # replace_last_assistant is what fires on a proven repeat, so the guarantee
    # is about the most recent turn: whatever the model copies from next must
    # not be the payload it already failed to parse. Earlier attempts stay as
    # evidence.
    assert TRUNCATED not in assistant[-1]


@pytest.mark.asyncio
async def test_the_full_raw_response_is_persisted_in_essential_mode(tmp_path: Path):
    """`essential` is the DEFAULT, and it dropped exactly the response needed."""
    ledger = ActionLedger(tmp_path)
    ledger.start(REQUEST)
    client = RepeatingClient(TRUNCATED)
    loop = _loop(tmp_path, client, ledger=ledger)

    await loop.run(REQUEST)

    saved = list((ledger.diagnostics_dir).glob("unparsed_response_*.txt"))
    assert saved, "the raw response was not persisted"
    body = saved[0].read_text(encoding="utf-8")
    # The WHOLE response, byte for byte - not the 300-char clip the terminal
    # message shows. The payload is deliberately longer than that clip so this
    # distinction is actually being tested.
    assert len(TRUNCATED) > 300
    assert body == TRUNCATED


@pytest.mark.asyncio
async def test_the_final_message_names_the_persisted_artifact(tmp_path: Path):
    ledger = ActionLedger(tmp_path)
    ledger.start(REQUEST)
    client = RepeatingClient(TRUNCATED)
    loop = _loop(tmp_path, client, ledger=ledger)

    result = await loop.run(REQUEST)

    assert "could not parse" in result.final
    assert "unparsed_response_" in result.final


@pytest.mark.asyncio
async def test_the_unparseable_event_is_logged_for_telemetry(tmp_path: Path):
    ledger = ActionLedger(tmp_path)
    ledger.start(REQUEST)
    client = RepeatingClient(TRUNCATED)
    loop = _loop(tmp_path, client, ledger=ledger)

    await loop.run(REQUEST)

    events = [
        json.loads(line)
        for line in (ledger.run_dir / ".evidence" / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]
    types = [e.get("type") for e in events]
    # Both, so the existing consumers keep working AND telemetry can reclassify.
    assert "mutation_tool_call_unparseable" in types
    assert "mutation_required_but_missing" in types
    assert "unparsed_model_response" in types


@pytest.mark.asyncio
async def test_the_2026_08_03_payload_now_writes_the_file_end_to_end(tmp_path: Path):
    """The whole point of slice 1, proven through the real loop.

    This exact response produced zero files in run_2026-08-03_00-49-07_037b. It
    must now reach disk on the FIRST round, with no correction needed.
    """
    client = RepeatingClient(REPAIRABLE)
    loop = _loop(tmp_path, client)

    result = await loop.run(REQUEST)

    written = tmp_path / "templates" / "my_orders.html"
    assert written.is_file(), "the file was still not written"
    body = written.read_text(encoding="utf-8")
    assert body.startswith('{% extends "base.html" %}')
    # The byte the model failed to escape has to survive to disk as-is.
    assert "href=\"{% url 'core:order_detail' order.id %}\">" in body
    assert body.rstrip().endswith("{% endblock %}")
    assert "templates/my_orders.html" in result.changed_files

    # And no honest-failure machinery should have been needed at all.
    assert not any(
        "could not parse" in text
        for messages in client.messages_seen
        for text in _user_messages(messages)
    )


@pytest.mark.asyncio
async def test_a_genuinely_prose_only_turn_still_gets_the_old_correction(tmp_path: Path):
    """The split must not collapse in the other direction."""
    client = RepeatingClient("I think you should edit that file yourself.")
    loop = _loop(tmp_path, client)

    await loop.run(REQUEST)

    prose_corrections = [
        text
        for messages in client.messages_seen
        for text in _user_messages(messages)
        if "No workspace mutation has succeeded yet" in text
    ]
    assert prose_corrections
    assert not any(
        "could not parse" in text
        for messages in client.messages_seen
        for text in _user_messages(messages)
    )

