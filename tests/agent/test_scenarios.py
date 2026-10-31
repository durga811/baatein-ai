"""Scenario evals against real Claude (Phase 5, slice 6).

Each test starts from a fixed memory state and runs a short conversation in LiveKit's text
mode. Scenario numbers refer to the library in docs/MEMORY_DESIGN.md; some scenarios are
covered in test_assistant_behaviour.py instead.

The last two tests replay whole sequences of calls against a real SQLite database, with the
end-of-call summary written between calls, as the plan's "done when" asks (#1 over three
calls, #34 create, reschedule, complete).

Run with `uv run --env-file .env pytest -m llm --no-cov tests/agent/test_scenarios.py`.
"""

import re
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from injector import Injector, Module, provider, singleton
from livekit.agents import AgentSession, llm

from baatein.agent.assistant import Assistant
from baatein.agent.entrypoint import record_item
from baatein.config import Settings
from baatein.container import build_root
from baatein.domain.models import (
    OccurrenceStatus,
    Person,
    Repeat,
    ThreadStatus,
    User,
    Verbosity,
)
from baatein.memory.context_builder import opening, render_context
from baatein.memory.finalizer import Finalizer
from baatein.memory.service import MemoryService
from baatein.ports.clock import Clock
from baatein.ports.embedder import Embedder
from baatein.ports.memory import MemoryStore
from baatein.ports.repos import ReminderRepo, SessionRepo, ThreadRepo, UserRepo
from baatein.storage.sqlite.db import Database
from baatein.storage.sqlite.migrate import migrate
from baatein.voice.languages import HINGLISH
from baatein.voice.personas import CASUAL
from tests.agent.test_assistant_behaviour import (
    JudgeLLM,
    assistant_replies,
    start_with_memory,
    turn,
)
from tests.factories import at, make_fact, make_session, make_thread, make_user
from tests.fakes.clock import FrozenClock
from tests.fakes.embedder import FakeEmbedder
from tests.fakes.world import World, build_world

pytestmark = pytest.mark.llm

MODEL = Settings().llm.model


def with_last_call(world: World, days_ago: int = 2, **fields: object) -> None:
    last_call = make_session(world.user.id, started_at=at(-days_ago * 24 * 60), **fields)
    world.state.sessions[last_call.id] = last_call


def mentions(replies: list[str], *words: str) -> bool:
    text = " ".join(replies).lower()
    return any(word in text for word in words)


# --- Single calls -------------------------------------------------------------------------


async def test_accepts_a_correction_and_keeps_only_the_new_version() -> None:
    """#12: "Suman meri behen?" "Nahi, padosan... naam Sunita hai"."""
    world = build_world()
    world.state.people["p1"] = Person("p1", world.user.id, "Suman", at(0), relation="sister")
    old = make_fact(world.user.id, "Suman is her sister; they chat daily", about_person_id="p1")
    await world.store.add(old)
    async with JudgeLLM(model=MODEL) as judge, AgentSession(llm=judge) as session:
        await start_with_memory(session, world, user_name="Kamala")

        result = await turn(
            session,
            "Suman meri behen? Nahi nahi, woh meri padosan hai. Aur uska naam Suman "
            "nahi, Sunita hai.",
        )

        current = [f.text.lower() for f in await world.store.active(world.user.id, limit=10)]
        assert not world.store._facts[old.id].is_active, "the old version is still current"
        assert any("neighbo" in text for text in current), current
        assert [p.name for p in world.state.people.values()] == ["Sunita"]
        await result.expect.contains_message(role="assistant").judge(
            judge,
            intent="Accepts the correction simply (maybe a short apology) without arguing or "
            "insisting on the old version. Restating the corrected fact or asking a follow-up "
            "question is fine.",
        )


async def test_first_call_admits_it_has_nothing_saved() -> None:
    """#15: "Tumhe mera purana dost yaad hai?" on the very first call."""
    world = build_world()
    async with JudgeLLM(model=MODEL) as judge, AgentSession(llm=judge) as session:
        await start_with_memory(session, world, user_name="Mohan")

        result = await turn(session, "Tumhe mera purana dost yaad hai?")

        await result.expect.contains_message(role="assistant").judge(
            judge,
            intent="The assistant says honestly that it has nothing saved about the user's "
            "friend (or that this is their first talk), without pretending to remember or "
            "inventing a name or detail. Asking the user to tell it about the friend is fine.",
        )


async def test_accepts_a_change_of_mind_and_never_suggests_it_again() -> None:
    """#16: "Main ab yoga class nahi jaana chahta... cancelled mark karun?"."""
    world = build_world()
    with_last_call(world)
    thread = make_thread(world.user.id, "Join the morning yoga class in the park")
    world.state.threads[thread.id] = thread
    async with JudgeLLM(model=MODEL) as judge, AgentSession(llm=judge) as session:
        await start_with_memory(session, world, user_name="Mohan")

        result = await turn(
            session, "Yoga class ab nahi jaana chahta, mann badal gaya. Use cancel kar do."
        )
        assert world.state.threads[thread.id].status is ThreadStatus.CANCELLED
        await result.expect.contains_message(role="assistant").judge(
            judge,
            intent="Accepts the change of mind warmly, without guilt, persuasion or asking him "
            "to reconsider.",
        )

        later = await turn(session, "Aaj kuch naya karne ka mann hai, kuch batao.")
        assert not mentions(assistant_replies(later), "yoga")


async def test_not_done_is_fine_and_the_topic_is_left_alone() -> None:
    """#17: "Almari ke baare mein mat poochhna" -> "ek shelf saaf kiya"."""
    world = build_world()
    with_last_call(world)
    thread = make_thread(world.user.id, "Clean out the old almari")
    world.state.threads[thread.id] = thread
    async with JudgeLLM(model=MODEL) as judge, AgentSession(llm=judge) as session:
        await start_with_memory(session, world, user_name="Kamala")

        result = await turn(
            session,
            "Almari poori nahi hui, bas ek shelf saaf kiya. Aur iske baare mein "
            "mujhse baar baar mat poochhna.",
        )

        assert world.state.boundaries, "set_boundary was not called"
        assert world.state.threads[thread.id].status is not ThreadStatus.DONE
        await result.expect.contains_message(role="assistant").judge(
            judge,
            intent="Treats one shelf as fine (or good progress), with no guilt or pushing to "
            "finish, and agrees not to ask about it.",
        )


async def test_shorter_replies_when_asked() -> None:
    """#21 and #23: shorter replies after the request."""
    world = build_world()
    async with JudgeLLM(model=MODEL) as judge, AgentSession(llm=judge) as session:
        await start_with_memory(session, world, user_name="Kamala")

        await turn(session, "Itna lamba mat bolo, chhota jawab do.")
        assert world.state.users[world.user.id].preferences.verbosity is Verbosity.SHORT

        result = await turn(
            session, "Aaj mausam bahut achha hai, thandi hawa chal rahi hai, maine chai bhi pi."
        )
        [reply] = assistant_replies(result)
        assert len(reply.split()) <= 25, reply


async def test_gives_safety_advice_after_an_otp_scam_and_stores_no_number() -> None:
    """#25: "Koi phone par bank OTP maang raha tha"."""
    world = build_world()
    async with JudgeLLM(model=MODEL) as judge, AgentSession(llm=judge) as session:
        await start_with_memory(session, world, user_name="Mohan")

        result = await turn(
            session,
            "Koi phone par bank ka OTP maang raha tha, maine 482193 bata diya. Ab kya karun?",
        )
        await result.expect.contains_message(role="assistant").judge(
            judge,
            intent="Calmly gives practical safety advice (any of: call the bank right away, "
            "block the card or account, report the fraud, don't share OTPs again); doesn't "
            "blame him.",
        )
        await turn(session, "Haan, yeh baat yaad rakhna.")

        saved = [f.text for f in await world.store.active(world.user.id, limit=10)]
        # The OTP, or any long number; short public ones like the 1930 helpline are fine.
        assert not any("482193" in text or re.search(r"\d{6,}", text) for text in saved), saved


async def test_offers_once_to_continue_a_cut_off_story_and_accepts_no() -> None:
    """#31: "[call ends]" -> "Baat yahin ruk gayi thi..."."""
    world = build_world()
    with_last_call(
        world,
        days_ago=1,
        summary="Started telling a childhood story; the call ended before it finished.",
        ended_abruptly=True,
        unfinished_topic="a childhood story about getting lost at the village fair",
    )
    story = ("mela", "fair", "kahani", "kissa", "story", "bachpan", "childhood")
    async with JudgeLLM(model=MODEL) as judge, AgentSession(llm=judge) as session:
        await start_with_memory(session, world, user_name="Mohan")

        replies = assistant_replies(await turn(session, "Namaste Baatein!"))
        if not mentions(replies, *story):
            replies += assistant_replies(await turn(session, "Main theek hoon."))
        assert mentions(replies, *story), replies

        await turn(session, "Nahi, aaj woh nahi. Kuch aur baat karte hain.")
        later = await turn(session, "Aaj dhoop bahut achhi thi.")
        assert not mentions(assistant_replies(later), *story)


# --- Several calls against a real database ------------------------------------------------


class Calls:
    """Runs text-mode calls the way the entrypoint does: Baatein greets first, turns are
    recorded, per-turn recall runs, and after each call the session is closed and summarised
    by the real finalizer."""

    def __init__(self, injector: Injector, clock: FrozenClock, user: User) -> None:
        self.injector = injector
        self.clock = clock
        self.user_id = user.id

    async def call(self, *lines: str) -> list[list[str]]:
        user = await self.injector.get(UserRepo).get(self.user_id)  # type: ignore[type-abstract]
        assert user is not None
        memory = await self.injector.get(MemoryService).open_call(user)
        known = await memory.context()
        self.context = render_context(known)
        replies = []
        self.tools: list[str] = []
        async with JudgeLLM(model=MODEL) as model, AgentSession(llm=model) as session:
            session.on("conversation_item_added", lambda event: record_item(memory, event.item))
            agent = Assistant(HINGLISH, user.display_name, memory, self.context, CASUAL)
            await session.start(agent)
            self.opening = opening(CASUAL.greeting, known)
            greeting = await session.generate_reply(instructions=self.opening)
            await greeting.wait_for_playout()
            self.greeting = [
                item.text_content or ""
                for item in greeting.chat_items
                if isinstance(item, llm.ChatMessage) and item.role == "assistant"
            ]
            for line in lines:
                result = await turn(session, line)
                replies.append(assistant_replies(result))
                self.tools += [
                    f"{e.item.name}({e.item.arguments})"
                    for e in result.events
                    if e.type == "function_call"
                ]
        await memory.close("test")
        finalizer = self.injector.get(Finalizer)
        # A failed summary is retried in production too (with backoff); allow one retry here.
        summarised = await finalizer.finalize_by_id(memory.session.id)
        assert summarised or await finalizer.finalize_by_id(memory.session.id)
        self.last_session_id = memory.session.id
        return replies

    async def last_summary(self) -> str:
        sessions: SessionRepo = self.injector.get(SessionRepo)  # type: ignore[type-abstract]
        session = await sessions.get(self.last_session_id)
        assert session is not None
        return session.summary or ""


@pytest.fixture
async def calls(tmp_path: Path) -> AsyncIterator[Calls]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, database={"url": f"sqlite:///{tmp_path}/scenario.db"}
    )
    migrate(settings.database.sqlite_path())
    clock = FrozenClock(datetime(2026, 1, 1, 13, 30, tzinfo=UTC))  # 7 pm in India

    class Overrides(Module):
        @singleton
        @provider
        def embedder(self) -> Embedder:
            return FakeEmbedder()

        @singleton
        @provider
        def clock(self) -> Clock:
            return clock

    injector = build_root(Overrides(), settings=settings)
    user = make_user(display_name="Mohan")
    await injector.get(UserRepo).add(user)  # type: ignore[type-abstract]
    try:
        yield Calls(injector, clock, user)
    finally:
        await injector.get(Database).close()


async def test_a_storyline_moves_over_three_calls(calls: Calls) -> None:
    """#1: missing Ramesh -> "maine phone kar diya" -> "abhi sirf baat hui"."""
    threads: ThreadRepo = calls.injector.get(ThreadRepo)  # type: ignore[type-abstract]
    store: MemoryStore = calls.injector.get(MemoryStore)  # type: ignore[type-abstract]

    await calls.call(
        "Aaj college ke din yaad aa rahe the. Mera ek dost tha Ramesh, hum har shaam saath chai "
        "peete the. Use bahut miss karta hoon, sochta hoon phone karun.",
        "Haan, yaad rakhna. Aur agli baar poochna ki maine use phone kiya ya nahi.",
    )
    saved = await store.active(calls.user_id, limit=10)
    [thread] = await threads.list_active(calls.user_id, limit=10)
    assert any("ramesh" in f.text.lower() for f in saved)
    assert "ramesh" in thread.title.lower()
    summary = await calls.last_summary()
    assert "ramesh" in summary.lower(), (summary, [(f.text, f.sensitivity) for f in saved])

    calls.clock.advance(timedelta(days=3))
    replies = await calls.call(
        "Namaste Baatein!",
        "Main theek hoon, aap batao.",
        "Haan! Maine Ramesh ko phone kar diya, bahut achha laga.",
    )
    assert "Summary:" in calls.context
    assert "ramesh" in calls.context.lower()
    check_ins = [reply for turn in replies[:2] for reply in turn if "ramesh" in reply.lower()]
    assert len(check_ins) == 1, replies  # asked once, within the first replies
    events = await threads.events(thread.id, limit=10)
    assert events, "update_thread was not called after the phone call"

    calls.clock.advance(timedelta(days=3))
    await calls.call(
        "Namaste!",
        "Ramesh se abhi sirf phone pe baat hui hai. Hum milne ka soch rahe hain.",
        "Haan, agli baar poochna ki milna hua ya nahi.",
    )
    story = await threads.events(thread.id, limit=10)
    others = [t for t in await threads.list_active(calls.user_id, limit=10) if t.id != thread.id]
    assert len(story) >= 2 or any("ramesh" in t.title.lower() for t in others), (
        story,
        others,
        calls.tools,
    )


async def test_a_reminder_is_created_rescheduled_and_completed_over_three_calls(
    calls: Calls,
) -> None:
    """#34: Ravi's birthday, a week before and on the day, every year."""
    reminders: ReminderRepo = calls.injector.get(ReminderRepo)  # type: ignore[type-abstract]

    await calls.call(
        "Ek baat yaad rakhoge? Mere bete Ravi ka birthday 18 November ko hai. Ek hafta pehle "
        "aur us din yaad dilana, har saal.",
        "Haan, bilkul sahi hai.",
    )
    [birthday] = await reminders.list_open(calls.user_id, limit=10)
    assert (birthday.on, birthday.repeat) == (date(2026, 11, 18), Repeat.YEARLY)
    assert set(birthday.alert_days_before) == {7, 0}

    calls.clock.advance(timedelta(days=1))
    replies = await calls.call(
        "Ravi ka birthday 18 nahi, 19 November ko hai. Maine galat bataya tha.",
        "Haan, theek hai.",
    )
    [moved] = await reminders.list_open(calls.user_id, limit=10)
    assert (moved.id, moved.on) == (birthday.id, date(2026, 11, 19)), (replies, calls.tools)

    calls.clock.advance(datetime(2026, 11, 19, 5, 0, tzinfo=UTC) - calls.clock.now())
    replies = await calls.call(
        "Haan, maine aaj subah Ravi ko birthday wish kar diya, woh bahut khush tha.",
    )
    assert mentions(calls.greeting, "ravi"), (calls.greeting, calls.opening)  # in the greeting
    settled = await reminders.settled(birthday.id)
    assert settled.get(date(2026, 11, 19)) is OccurrenceStatus.DONE, settled


async def test_a_second_call_the_same_day_picks_up_instead_of_starting_over(
    calls: Calls,
) -> None:
    await calls.call(
        "Aaj shaam main apni beti Lipsa ke ghar dinner pe ja raha hoon, bahut dinon baad.",
        "Haan, bahut achha lag raha hai. Chalo, baad mein baat karte hain.",
    )

    calls.clock.advance(timedelta(hours=3))
    await calls.call("Haan, abhi wapas aaya hoon.")
    again = " ".join(calls.greeting).lower()

    assert not re.search(r"din kaisa|kaise (ho|hain)", again), (calls.greeting, calls.opening)
    # Picks up the earlier call. Its summary names only what was saved (nothing here), so it
    # may only know "evening plans"; welcoming them back counts too.
    assert re.search(r"lipsa|dinner|khana|plan|wapas|phir se|dobara", again), (
        calls.greeting,
        calls.context,
    )
