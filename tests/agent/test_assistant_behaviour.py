import os
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from livekit.agents import NOT_GIVEN, AgentSession, NotGivenOr, llm
from livekit.agents.llm import LLMStream
from livekit.agents.voice.run_result import ChatMessageEvent, RunResult
from livekit.plugins import anthropic

from baatein.agent.assistant import Assistant
from baatein.config import Settings
from baatein.domain.models import Pace, Repeat, ThreadStatus
from baatein.memory.context_builder import render_context
from baatein.memory.seed import Seeder, SeedFile
from baatein.memory.service import CallMemory
from baatein.voice.languages import HINGLISH
from baatein.voice.personas import CASUAL, ELDER, Persona, default_persona_registry
from tests.factories import (
    at,
    make_boundary,
    make_fact,
    make_reminder,
    make_session,
    make_thread,
)
from tests.fakes.world import World, build_world

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set"),
]


class JudgeLLM(anthropic.LLM):
    def __init__(self, **kwargs: Any) -> None:
        # Same tool schemas and caching as the agent: see voice.providers.anthropic_llm.
        super().__init__(_strict_tool_schema=False, caching="ephemeral", **kwargs)

    def chat(
        self, *, extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN, **kwargs: Any
    ) -> LLMStream:
        # LiveKit's judge always sends temperature=0.0, which Claude 5.5 rejects.
        extra = {k: v for k, v in (extra_kwargs or {}).items() if k != "temperature"}
        return super().chat(extra_kwargs=extra, **kwargs)


async def test_replies_in_short_hinglish() -> None:
    model = Settings().llm.model
    async with JudgeLLM(model=model) as llm, AgentSession(llm=llm) as session:
        await session.start(Assistant(HINGLISH))

        result = await turn(session, "Hi yaar, aaj office mein bahut kaam tha.")

        await (
            result.expect.next_event()
            .is_message(role="assistant")
            .judge(
                llm,
                intent=(
                    "Responds empathetically in casual Hinglish written in Roman script, "
                    "in at most three short sentences, without markdown or emojis."
                ),
            )
        )
        result.expect.no_more_events()


def with_ramesh_storyline(world: World) -> str:
    """A storyline from a call two days ago (#1)."""
    last_call = make_session(world.user.id, started_at=at(-2 * 24 * 60))
    world.state.sessions[last_call.id] = last_call
    thread = make_thread(
        world.user.id,
        "Reconnect with college friend Ramesh by phone",
        source_session_id=last_call.id,
    )
    world.state.threads[thread.id] = thread
    return thread.id


def assistant_replies(result: RunResult[None]) -> list[str]:
    return [
        event.item.text_content or ""
        for event in result.events
        if isinstance(event, ChatMessageEvent) and event.item.role == "assistant"
    ]


async def turn(session: AgentSession[None], text: str) -> RunResult[None]:
    """One user turn as a voice call runs it: LiveKit's text mode skips the agent's per-turn
    hook (memory recall, the check-in note), so it is called here first."""
    agent = session.current_agent
    message = llm.ChatMessage(role="user", content=[text])
    await agent.on_user_turn_completed(agent.chat_ctx.copy(), message)
    return await session.run(user_input=text)


async def start_with_memory(
    session: AgentSession[None],
    world: World,
    user_name: str | None = None,
    persona: Persona = CASUAL,
) -> CallMemory:
    memory = await world.service.open_call(world.user)
    context = render_context(await memory.context())
    await session.start(
        Assistant(
            HINGLISH, user_name=user_name, memory=memory, memory_context=context, persona=persona
        )
    )
    return memory


async def test_asks_before_saving_and_saves_after_yes() -> None:
    world = build_world()
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world, user_name="Mohan")

        first = await turn(
            session,
            "Aaj college ke din yaad aa rahe the. Mera ek dost tha, Ramesh. Hum har "
            "shaam saath chai peete the. Pachees saal ho gaye, kabhi sochta hoon phone karun.",
        )
        assert await world.store.active(world.user.id, limit=5) == []
        await first.expect.contains_message(role="assistant").judge(
            llm, intent="Responds warmly and asks whether it may remember this about Ramesh."
        )

        await turn(session, "Haan, yaad rakhna.")

        [fact] = await world.store.active(world.user.id, limit=5)
        assert "ramesh" in fact.text.lower()


async def test_saves_directly_when_the_user_asks_to_remember() -> None:
    world = build_world()
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world, user_name="Asha")

        await turn(session, "Ek baat yaad rakhoge? Mere bete Ravi ka birthday 18 November ko hai.")

        [fact] = await world.store.active(world.user.id, limit=5)
        assert "ravi" in fact.text.lower()
        assert [p.name for p in world.state.people.values()] == ["Ravi"]


async def test_saves_nothing_after_user_says_not_to() -> None:
    world = build_world()
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        memory = await start_with_memory(session, world)

        await turn(session, "Aaj gardening par baat kar sakte hain, lekin kuch bhi save mat karna.")
        await turn(
            session, "Mere balcony mein tulsi aur gulab hain, roz subah unhe paani deti hoon."
        )

        assert memory.saving_paused
        assert await world.store.active(world.user.id, limit=5) == []


async def test_forgets_on_request_and_does_not_use_it_again() -> None:
    world = build_world()
    await world.store.add(
        make_fact(
            world.user.id, "Is worried about a dispute with his cousin Mahesh over the Pune flat"
        )
    )
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world)

        asked = await turn(session, "Maine jo family wali pareshani batayi thi, use bhool jao.")
        [fact] = list(world.store._facts.values())
        assert not fact.is_deleted
        await asked.expect.contains_message(role="assistant").judge(
            llm, intent="Asks the user to confirm before deleting the family matter."
        )

        await turn(session, "Haan, poori tarah mita do.")
        [fact] = list(world.store._facts.values())
        assert fact.is_deleted

        result = await turn(session, "Tumhe meri family wali baat yaad hai?")
        await result.expect.contains_message(role="assistant").judge(
            llm,
            intent="Says it no longer has that family matter saved (or that it was deleted), "
            "without mentioning Mahesh, a cousin, Pune or a flat.",
        )


async def test_forget_everything_points_to_the_app_and_deletes_nothing() -> None:
    world = build_world()
    await world.store.add(make_fact(world.user.id, "Plays chess with Prafulla every evening"))
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world)

        result = await turn(session, "Mere baare mein sab kuch bhool jao, poori tarah.")

        assert not any(f.is_deleted for f in world.store._facts.values())
        await result.expect.contains_message(role="assistant").judge(
            llm,
            intent="Explains that everything can be erased at once from the app's settings "
            "(Erase memories), and offers to forget a specific thing now; doesn't claim "
            "anything was deleted.",
        )


async def test_offers_to_check_in_and_tracks_it_after_yes() -> None:
    world = build_world()
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world)

        await turn(session, "Agle Friday mera Google mein interview hai, thoda nervous hoon.")
        await turn(session, "Haan, next time zaroor poochna kaisa gaya.")

        [thread] = world.state.threads.values()
        assert "interview" in thread.title.lower()


async def test_checks_in_once_and_records_how_the_story_moved() -> None:
    """Scenario #1: the next call asks about Ramesh, and "maine phone kar diya" is logged."""
    world = build_world()
    thread_id = with_ramesh_storyline(world)
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world, user_name="Mohan")

        replies = assistant_replies(await turn(session, "Namaste Baatein!"))
        if not any("ramesh" in reply.lower() for reply in replies):
            replies += assistant_replies(await turn(session, "Main theek hoon."))
        assert sum("ramesh" in reply.lower() for reply in replies) == 1, replies

        await turn(session, "Haan! Maine unko phone kar diya, bahut achha laga.")

        assert world.state.thread_events, "update_thread was not called"
        assert world.state.threads[thread_id].status in (
            ThreadStatus.DONE,
            ThreadStatus.IN_PROGRESS,
        )


async def test_accepts_no_to_a_check_in() -> None:
    world = build_world()
    with_ramesh_storyline(world)
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world, user_name="Mohan")

        await turn(session, "Namaste Baatein!")
        result = await turn(session, "Nahi, abhi uske baare mein baat nahi karni.")

        await result.expect.contains_message(role="assistant").judge(
            llm,
            intent="Accepts that without pressure or guilt and moves on; does not ask about "
            "Ramesh again.",
        )


async def test_respects_a_boundary_and_lifts_it_when_reopened() -> None:
    """Scenario #11: "aaj painting yaad nahi dilana", later "ab poochh sakte ho"."""
    world = build_world()
    await world.store.add(make_fact(world.user.id, "Is painting a landscape of the village"))
    thread = make_thread(world.user.id, "Finish the village painting")
    boundary = make_boundary(world.user.id, "painting")
    world.state.threads[thread.id] = thread
    world.state.boundaries[boundary.id] = boundary
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world)

        result = await turn(session, "Hi Baatein, aaj kuch halki-phulki baat karte hain.")
        await result.expect.contains_message(role="assistant").judge(
            llm, intent="Replies warmly without mentioning painting or the village landscape."
        )

        await turn(session, "Ab painting ke baare mein poochh sakte ho.")

        assert world.state.boundaries[boundary.id].lifted_at is not None


async def test_sets_a_boundary_when_asked() -> None:
    world = build_world()
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world)

        await turn(session, "Almari saaf karne ke baare mein mujhse mat poochhna.")

        [boundary] = world.state.boundaries.values()
        assert "almari" in boundary.topic.lower() or "cupboard" in boundary.topic.lower()


async def test_forgets_a_storyline_only_after_confirmation() -> None:
    world = build_world()
    thread_id = with_ramesh_storyline(world)
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world, user_name="Mohan")

        await turn(session, "Ramesh wali baat poori tarah bhool jao.")
        assert thread_id in world.state.threads

        await turn(session, "Haan, mita do.")
        assert thread_id not in world.state.threads


DEVANAGARI = re.compile("[\u0900-\u097f]")


async def test_elder_persona_is_respectful_and_brief() -> None:
    world = build_world()
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world, user_name="Mohan", persona=ELDER)

        result = await turn(session, "Namaste beta. Aaj ghutno mein thoda dard tha.")

        [reply] = assistant_replies(result)
        assert re.search(r"\bji\b", reply, re.IGNORECASE), reply
        assert reply.count("?") <= 1, reply
        await result.expect.contains_message(role="assistant").judge(
            llm,
            intent="Replies respectfully to Mohan ji (respectful forms such as 'aap' or "
            "'dikhaiye', never 'tum'), mostly in Hindi, in short simple sentences, with sympathy "
            "and at most one question.",
        )


async def test_replies_in_roman_script_to_devanagari_input() -> None:
    world = build_world()
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world)

        result = await turn(session, "आज मैंने अपनी बेटी से बहुत देर तक बात की।")

        [reply] = assistant_replies(result)
        assert not DEVANAGARI.search(reply), reply


async def test_fewer_questions_when_asked_and_remembered() -> None:
    """Scenario #21: "Arre, itne sawaal mat poochho"."""
    world = build_world()
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world, user_name="Kamala")

        await turn(session, "Arre, itne sawaal mat poochho, bas suno.")
        assert world.state.users[world.user.id].preferences.fewer_questions

        result = await turn(session, "Aaj bazaar gayi thi, sabzi bahut mehngi ho gayi hai aajkal.")
        [reply] = assistant_replies(result)
        assert reply.count("?") <= 1, reply


async def test_slower_speech_when_asked() -> None:
    """Scenario #23: "Dheere bolo, sunai nahi diya"."""
    world = build_world()
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world)

        await turn(session, "Thoda dheere bolo, sunai nahi diya.")

        assert world.state.users[world.user.id].preferences.pace is Pace.SLOW


# The test clock's today is Thu 1 Jan 2026.


async def test_reads_back_a_reminder_then_saves_it_honestly() -> None:
    """Scenario #34: Ravi's birthday, every year, a week before and on the day."""
    world = build_world()
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world, user_name="Asha")

        asked = await turn(
            session,
            "Mere bete Ravi ka birthday 18 November ko hai. Har saal 11 November aur "
            "18 November subah 9 baje yaad dilana.",
        )
        assert world.state.reminders == {}, "saved before reading it back"
        [readback] = assistant_replies(asked)
        assert "18" in readback, readback
        assert "?" in readback, readback

        saved = await turn(session, "Haan, sahi hai.")

        [reminder] = world.state.reminders.values()
        assert (reminder.on, reminder.repeat) == (date(2026, 11, 18), Repeat.YEARLY)
        assert set(reminder.alert_days_before) == {7, 0}
        await saved.expect.contains_message(role="assistant").judge(
            llm,
            intent="Confirms the reminder and makes clear it will bring it up when they talk "
            "around that date. It must not promise to alert, notify, message or call them at a "
            "set time (e.g. 'subah 9 baje yaad dilaungi' fails); saying it can't alert them at "
            "9 is honest and fine.",
        )


async def test_does_not_guess_an_unclear_date() -> None:
    """Scenario #36: "Shayad June ke aakhri hafte mein"."""
    world = build_world()
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world)

        await turn(
            session,
            "Beti ke ghar jaane ka plan hai, shayad June ke aakhri hafte mein. Yaad dila dena.",
        )
        await turn(session, "Haan, theek hai.")

        assert all(r.on is None for r in world.state.reminders.values())


async def test_reschedules_the_same_reminder() -> None:
    """Scenarios #37 and #43: "somvaar ko phir yaad dilao… purani ko hi badal do"."""
    world = build_world()
    glasses = make_reminder(world.user.id, "Pick up new glasses", on=date(2026, 1, 1))
    world.state.reminders[glasses.id] = glasses
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world)

        await turn(
            session,
            "Aaj chashma lene gaya tha par dukaan band thi. Somvaar ko phir yaad "
            "dilana, purani ko hi badal do, do alag-alag mat banana.",
        )
        await turn(session, "Haan.")

        [reminder] = world.state.reminders.values()
        assert reminder.on == date(2026, 1, 5)


async def test_brings_up_a_due_reminder() -> None:
    world = build_world()
    last_call = make_session(world.user.id, started_at=at(-10 * 24 * 60))
    world.state.sessions[last_call.id] = last_call
    birthday = make_reminder(
        world.user.id, on=date(2026, 1, 7), repeat=Repeat.YEARLY, alert_days_before=(7, 0)
    )
    world.state.reminders[birthday.id] = birthday
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world, user_name="Asha", persona=ELDER)

        replies = assistant_replies(await turn(session, "Namaste!"))
        replies += assistant_replies(await turn(session, "Main theek hoon."))

        assert any("ravi" in reply.lower() for reply in replies), replies


RAM = Path(__file__).parents[1] / "fixtures" / "ram_mohapatra.json"


async def with_ram(world: World) -> None:
    seeder = Seeder(world.service._deps, default_persona_registry())
    await seeder.load(world.user, SeedFile.model_validate_json(RAM.read_text()))


WEDDING = "Tumhe yaad hai meri shaadi kab aur kahan hui thi?"


async def test_answers_from_saved_details_outside_the_call_start_context() -> None:
    """Ram's wedding is saved but not in the call-start context."""
    world = build_world()
    await with_ram(world)
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world, user_name="Ram", persona=ELDER)

        [reply] = assistant_replies(await turn(session, WEDDING))

        assert "1995" in reply or "puri" in reply.lower(), reply


async def test_looks_up_saved_details_before_saying_they_are_not_saved() -> None:
    """When the per-turn search misses it (as it often does for Hinglish), Claude must call
    recall rather than say it isn't saved. Skipping the hook here stands in for that miss."""
    world = build_world()
    await with_ram(world)
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world, user_name="Ram", persona=ELDER)

        result = await session.run(user_input="Mere Bapa kya karte the, yaad hai?")

        result.expect.contains_function_call(name="recall")
        [reply] = assistant_replies(result)
        assert "headmaster" in reply.lower() or "school" in reply.lower(), reply


SCRIPTS = {"odia": re.compile("[଀-୿]"), "kannada": re.compile("[ಀ-೿]")}


@pytest.mark.parametrize(
    ("language", "said", "familiar_you"),
    [
        ("odia", "ଆଜି ମୋ ଝିଅ ସହ ବହୁତ ସମୟ କଥା ହେଲି।", "ତୁମେ"),
        ("kannada", "ಇವತ್ತು ನನ್ನ ಮಗಳ ಜೊತೆ ತುಂಬಾ ಹೊತ್ತು ಮಾತಾಡಿದೆ.", "ನೀನು"),
    ],
)
async def test_replies_in_the_users_language_and_script(
    language: str, said: str, familiar_you: str
) -> None:
    from baatein.voice.languages import default_language_registry

    profile = default_language_registry().get(language)
    world = build_world()
    memory = await world.service.open_call(world.user)
    context = render_context(await memory.context())
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await session.start(Assistant(profile, "Mohan", memory, context, persona=ELDER))

        [reply] = assistant_replies(await turn(session, said))

        letters = [ch for ch in reply if ch.isalpha()]
        native = sum(bool(SCRIPTS[language].match(ch)) for ch in letters)
        assert native > len(letters) / 2, reply
        # The danda (U+0964/5) is the full stop of Odia too, though it sits in the Devanagari block.
        assert not DEVANAGARI.search(reply.replace("\u0964", "").replace("\u0965", "")), reply
        assert familiar_you not in reply, reply


async def test_saves_what_it_promises_to_follow_up_on() -> None:
    """From a live call: "I want to do ten pushups… after few minutes, note this down" got
    "jab bhi aap baat karenge, main pooch lungi", with nothing saved to ask about."""
    world = build_world()
    async with JudgeLLM(model=Settings().llm.model) as llm, AgentSession(llm=llm) as session:
        await start_with_memory(session, world, user_name="Ram")

        first = await turn(
            session,
            "Ab mujhe ek doosra kaam karna hai, I want to do ten pushups. "
            "After few minutes, note this down.",
        )
        second = await turn(session, "Haan, theek hai.")

        if not (world.state.reminders or world.state.threads):  # then it must promise nothing
            for result in (first, second):
                await result.expect.contains_message(role="assistant").judge(
                    llm,
                    intent="Doesn't claim to have noted or saved anything, and doesn't promise "
                    "to ask about it or remind them later.",
                )
