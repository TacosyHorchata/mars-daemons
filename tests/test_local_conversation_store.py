from __future__ import annotations

import asyncio

from mars_runtime.core.store import ConversationContext, PersistedState, UsageMetrics
from mars_runtime.host.stores.conversation_store import LocalConversationStore


def test_local_conversation_store_round_trip(tmp_path):
    async def scenario() -> None:
        store = LocalConversationStore(tmp_path)
        conversation_id = await store.create("org-1", "user-1")

        persisted = PersistedState(
            context=ConversationContext(
                messages=[{"role": "system", "content": "base"}],
                conversation=[{"role": "user", "content": "hola", "type": "user_message"}],
                _event_sequence=2,
                _durable_events=[
                    {"sequence": 1, "type": "agent_message"},
                    {"sequence": 2, "type": "turn_completed"},
                ],
            ),
            status="idle",
            usage=UsageMetrics(llm_calls=1, input_tokens=10, output_tokens=5, tool_calls=0),
            last_message_at="2026-01-01T00:00:00+00:00",
        )
        await store.save(conversation_id, persisted, org_id="org-1")

        loaded = await store.load(conversation_id, org_id="org-1")
        assert loaded is not None
        assert loaded.context.messages[0]["content"] == "base"
        assert loaded.usage.llm_calls == 1

        details = await store.get(conversation_id, org_id="org-1", created_by="user-1")
        assert details is not None
        assert details["status"] == "idle"

        events = await store.list_durable_events(conversation_id, after_sequence=1, org_id="org-1")
        assert [event["sequence"] for event in events] == [2]

    asyncio.run(scenario())
