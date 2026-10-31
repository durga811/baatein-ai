import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from baatein.domain.models import (
    BoundaryScope,
    Certainty,
    MemoryKind,
    Pace,
    Preferences,
    ReminderStatus,
    Repeat,
    Sensitivity,
    ThreadStatus,
)
from baatein.memory.seed import SAVED_AFTER, Seeder, SeedError, SeedFile, resolve_day
from baatein.voice.personas import UnknownPersonaError, default_persona_registry
from tests.fakes.world import World, build_world

EXAMPLE = Path(__file__).parents[1] / "fixtures" / "mohan.json"
IST = ZoneInfo("Asia/Kolkata")
TODAY = date(2026, 1, 1)  # the test clock, in India


def seeder(world: World) -> Seeder:
    return Seeder(world.service._deps, default_persona_registry())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("today", TODAY),
        ("today+5", date(2026, 1, 6)),
        ("today-2", date(2025, 12, 30)),
        ("2026-11-18", date(2026, 11, 18)),
        ("next sunday", date(2026, 1, 4)),
        ("Next Thursday", date(2026, 1, 8)),
    ],
)
def test_resolve_day(value: str, expected: date) -> None:
    assert resolve_day(value, TODAY) == expected


@pytest.mark.parametrize(
    "bad",
    [
        {"reminders": [{"title": "x", "date": "someday"}]},
        {"reminders": [{"title": "x", "time": "9 baje"}]},
        {"threads": [{"title": "x", "check_in_after": "tomorrow"}]},
        {"boundaries": [{"topic": "x", "scope": "this_call"}]},
        {"memories": [{"text": "x", "importance": 9}]},
        {"memories": [{"txt": "typo"}]},
    ],
)
def test_bad_files_are_rejected(bad: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValidationError):
        SeedFile.model_validate(bad)


async def test_example_seed_file_loads_into_a_full_context() -> None:
    world = build_world()
    seed = SeedFile.model_validate(json.loads(EXAMPLE.read_text()))

    result = await seeder(world).load(world.user, seed)

    assert (result.people, result.calls, result.memories, result.threads) == (3, 2, 4, 3)
    assert (result.boundaries, result.declined_suggestions, result.reminders) == (1, 1, 3)
    user = world.state.users[world.user.id]
    assert (user.persona, user.preferences) == ("elder", Preferences(fewer_questions=True))
    memory = await world.service.open_call(user)
    context = await memory.context()
    assert context.last_call is not None
    assert context.last_call.unfinished_topic is not None
    assert context.check_in is None
    assert {t.title for t in context.threads} == {
        "Reconnect with college friend Ramesh by phone",
        "Finish the village painting",
    }
    assert [d.reminder.title for d in context.reminders] == ["Ravi's birthday"]
    assert context.reminders[0].alerted
    assert [r.title for r in context.undated_reminders] == ["Visit daughter in June"]
    assert [b.topic for b in context.boundaries] == ["painting"]
    found = await memory.search("Ramesh chai")
    assert found[0].fact.quote == "Hum har shaam saath chai peete the"


async def test_details_are_stored_as_given() -> None:
    world = build_world()
    seed = SeedFile.model_validate(
        {
            "people": [{"name": "Ravi"}, {"name": "ravi", "relation": "son"}],
            "calls": [
                {
                    "key": "c",
                    "days_ago": 3,
                    "minutes": 5,
                    "turns": [
                        {"role": "user", "text": "Namaste"},
                        {"role": "assistant", "text": "Namaste ji"},
                    ],
                }
            ],
            "memories": [
                {
                    "text": "Gets dizzy",
                    "kind": "story",
                    "sensitivity": "health",
                    "uncertain": True,
                    "call": "c",
                    "about": "Ravi",
                }
            ],
            "threads": [
                {
                    "title": "Yoga",
                    "status": "snoozed",
                    "check_in_after": "today+2",
                    "sensitivity": "health",
                    "events": [{"text": "Paused", "days_ago": 4}],
                }
            ],
            "boundaries": [
                {"topic": "almari", "scope": "until_date", "until": "today+3"},
                {"topic": "odd", "scope": "until_date"},
            ],
            "reminders": [
                {
                    "title": "Bill",
                    "date": "today",
                    "time": "18:30",
                    "repeat": "monthly_date",
                    "days_before": [0, 3, 3],
                }
            ],
            "preferences": {"pace": "slow"},
        }
    )

    result = await seeder(world).load(world.user, seed)

    assert result.people == 1
    [person] = world.state.people.values()
    assert person.relation == "son"
    [call] = world.state.sessions.values()
    assert call.started_at == world.clock.now() - timedelta(days=3, minutes=5)
    assert [t.text for t in world.state.turns] == ["Namaste", "Namaste ji"]
    [fact] = await world.store.active(world.user.id, limit=5)
    assert (fact.kind, fact.sensitivity, fact.certainty, fact.source_session_id) == (
        MemoryKind.STORY,
        Sensitivity.HEALTH,
        Certainty.UNCERTAIN,
        call.id,
    )
    [thread] = world.state.threads.values()
    assert thread.status is ThreadStatus.SNOOZED
    assert thread.check_in_after is not None
    assert thread.check_in_after.date() == date(2026, 1, 3)
    assert thread.check_in_after.tzinfo == IST
    assert thread.created_at == world.clock.now()
    assert world.state.thread_events[0].created_at == world.clock.now() - timedelta(days=4)
    dated, undated = world.state.boundaries.values()
    assert (dated.scope, dated.until.date() if dated.until else None) == (
        BoundaryScope.UNTIL_DATE,
        date(2026, 1, 4),
    )
    assert (undated.scope, undated.until) == (BoundaryScope.UNTIL_REOPENED, None)
    [bill] = world.state.reminders.values()
    assert (bill.on, bill.at, bill.repeat, bill.alert_days_before, bill.status) == (
        TODAY,
        time(18, 30),
        Repeat.MONTHLY_DATE,
        (3, 0),
        ReminderStatus.ACTIVE,
    )
    assert world.state.users[world.user.id].preferences.pace is Pace.SLOW


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        ({"memories": [{"text": "x", "call": "nope"}]}, "unknown call 'nope'"),
        ({"calls": [{"key": "a", "days_ago": 1}] * 2}, "call keys must be unique"),
        ({"threads": [{"key": "t", "title": "x"}] * 2}, "storyline keys must be unique"),
        ({"memories": [{"text": "y", "corrects": "x"}]}, "unknown memory 'x'"),
        ({"memories": [{"key": "a", "text": "x"}] * 2}, "memory key 'a' is used twice"),
        ({"boundaries": [{"topic": "x", "thread": "t"}]}, "unknown storyline 't'"),
        ({"reminders": [{"title": "x", "settled": [{"date": "soon", "status": "done"}]}]}, "soon"),
    ],
)
def test_a_file_with_broken_references_is_refused_before_loading(
    data: dict[str, object], problem: str
) -> None:
    with pytest.raises(ValidationError, match=problem):
        SeedFile.model_validate(data)


async def test_an_unknown_persona_is_refused() -> None:
    world = build_world()

    with pytest.raises(UnknownPersonaError):
        await seeder(world).load(world.user, SeedFile(persona="pirate"))


async def test_a_call_later_today_than_now_ends_now() -> None:
    world = build_world()

    await seeder(world).load(
        world.user, SeedFile.model_validate({"calls": [{"days_ago": 0, "at": "23:59"}]})
    )

    [call] = world.state.sessions.values()
    assert call.ended_at == world.clock.now()


async def test_corrections_call_links_offers_and_declined_objects() -> None:
    world = build_world()
    seed = SeedFile.model_validate(
        {
            "calls": [
                {"key": "c1", "days_ago": 7, "at": "19:00"},
                {"key": "c2", "days_ago": 2, "at": "18:30"},
            ],
            "memories": [
                {"key": "noida", "text": "Lipsa lives in Noida", "call": "c1"},
                {"corrects": "noida", "text": "Lipsa lives in Dwarka", "call": "c2"},
            ],
            "threads": [
                {
                    "title": "December visit",
                    "call": "c1",
                    "last_offered_days_ago": 1,
                    "events": [{"text": "Might come", "call": "c1"}, {"text": "Confirmed"}],
                }
            ],
            "boundaries": [{"topic": "zameen", "call": "c1"}],
            "declined_suggestions": [{"text": "laughter yoga", "call": "c2"}, "walks"],
            "reminders": [{"title": "Video call", "date": "next sunday", "call": "c2"}],
        }
    )

    await seeder(world).load(world.user, seed)

    first, second = sorted(world.state.sessions.values(), key=lambda c: c.started_at)
    assert first.started_at == datetime(2025, 12, 25, 19, 0, tzinfo=IST)
    [current] = await world.store.active(world.user.id, limit=5)
    assert (current.text, current.source_session_id) == ("Lipsa lives in Dwarka", second.id)
    [thread] = world.state.threads.values()
    assert (thread.times_offered, thread.last_offered_at) == (
        1,
        world.clock.now() - timedelta(days=1),
    )
    might, confirmed = world.state.thread_events
    assert (might.session_id, might.created_at) == (first.id, first.started_at + SAVED_AFTER)
    assert (confirmed.session_id, confirmed.created_at) == (first.id, world.clock.now())
    [boundary] = world.state.boundaries.values()
    assert boundary.session_id == first.id
    assert [(d.text, d.session_id) for d in world.state.suggestions] == [
        ("laughter yoga", second.id),
        ("walks", None),
    ]
    [video] = world.state.reminders.values()
    assert (video.on, video.source_session_id) == (date(2026, 1, 4), second.id)


async def test_create_user_from_the_user_section() -> None:
    world = build_world()
    seed = SeedFile.model_validate(
        {
            "user": {"email": " Ram@Example.com ", "password": "long-enough", "timezone": "UTC"},
            "persona": "elder",
            "preferences": {"address_as": "Ram Babu"},
            "calls": [{"days_ago": 21}],
        }
    )

    user = await seeder(world).create_user(seed, "hashed")

    assert world.state.users[user.id] == user
    assert (user.email, user.password_hash, user.timezone, user.persona) == (
        "ram@example.com",
        "hashed",
        "UTC",
        "elder",
    )
    assert user.preferences == Preferences(address_as="Ram Babu")
    assert user.created_at == world.clock.now() - timedelta(days=22)
    plain = await seeder(world).create_user(
        SeedFile.model_validate({"user": {"email": "b@x.com", "password": "long-enough"}}), "h"
    )
    assert (plain.persona, plain.preferences) == ("casual", Preferences())
    with pytest.raises(SeedError, match="no 'user' section"):
        await seeder(world).create_user(SeedFile(), "h")


async def test_explicit_nulls_and_unnamed_calls_are_fine() -> None:
    world = build_world()
    seed = SeedFile.model_validate(
        {
            "calls": [{"days_ago": 1, "at": None}],
            "threads": [{"title": "x", "check_in_after": None}],
            "boundaries": [{"topic": "y", "until": None}],
            "reminders": [{"title": "z", "date": None}],
        }
    )

    result = await seeder(world).load(world.user, seed)

    assert (result.calls, result.threads, result.boundaries, result.reminders) == (1, 1, 1, 1)
