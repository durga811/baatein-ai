"""Ram Mohapatra's seed (four past calls) sets up the next call the way the scenarios need."""

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from baatein.config import MemorySettings
from baatein.domain.models import ReminderStatus, ThreadKind, User
from baatein.memory.seed import Seeder, SeedFile
from baatein.memory.service import MemoryContext, MemoryDeps, MemoryService
from baatein.storage.sqlite.db import Database
from baatein.storage.sqlite.memory_store import SqliteMemoryStore
from baatein.storage.sqlite.migrate import migrate
from baatein.storage.sqlite.repos import (
    SqliteBoundaryRepo,
    SqlitePeopleRepo,
    SqliteReminderRepo,
    SqliteSessionRepo,
    SqliteSuggestionRepo,
    SqliteThreadRepo,
    SqliteTurnRepo,
    SqliteUserRepo,
    delete_user,
)
from baatein.voice.personas import default_persona_registry
from tests.fakes.clock import FrozenClock
from tests.fakes.embedder import FakeEmbedder

RAM = Path(__file__).parents[1] / "fixtures" / "ram_mohapatra.json"
NOW = datetime(2026, 10, 30, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    path = tmp_path / "app.db"
    migrate(path)
    database = Database(path)
    yield database
    await database.close()


def deps(db: Database) -> MemoryDeps:
    return MemoryDeps(
        SqliteUserRepo(db),
        SqliteSessionRepo(db),
        SqliteTurnRepo(db),
        SqlitePeopleRepo(db),
        SqliteThreadRepo(db),
        SqliteBoundaryRepo(db),
        SqliteSuggestionRepo(db),
        SqliteReminderRepo(db),
        SqliteMemoryStore(db, FakeEmbedder()),
        FrozenClock(NOW),
        MemorySettings(),
    )


async def seed_ram(db: Database) -> User:
    seeder = Seeder(deps(db), default_persona_registry())
    seed = SeedFile.model_validate_json(RAM.read_text())
    user = await seeder.create_user(seed, "hash")
    await seeder.load(user, seed)
    return user


async def next_call(db: Database, user: User) -> MemoryContext:
    """The context the agent would get on Ram's next call."""
    memory = await MemoryService(deps(db), FakeEmbedder()).open_call(user)
    return await memory.context()


async def test_ram_seed_sets_up_his_next_call(db: Database) -> None:
    user = await seed_ram(db)
    context = await next_call(db, user)

    assert user.persona == "elder"
    assert user.preferences.address_as == "Ram Babu"
    assert context.last_call is not None
    assert context.last_call.summary
    assert len(context.facts) == 10
    texts = " ".join(f.text for f in context.facts)
    assert "Dwarka" in texts
    assert "Noida" not in texts
    assert context.check_in is not None
    assert "December" in context.check_in.title
    assert len(context.check_in_events) == 2
    assert {t.kind for t in context.threads} >= {ThreadKind.WORRY, ThreadKind.ASPIRATION}
    assert [b.topic for b in context.boundaries] == ["Laxman aur gaon ki zameen ka jhagda"]
    assert [d.reminder.title for d in context.reminders if d.alerted] == ["Sambit's birthday"]
    assert [r.status for r in context.undated_reminders] == [ReminderStatus.NEEDS_INFO]
    assert "Laxman" not in {p.name for p in context.people}


async def test_ram_memories_outside_the_context_are_recallable(db: Database) -> None:
    user = await seed_ram(db)

    hits = await SqliteMemoryStore(db, FakeEmbedder()).search(user.id, "Bijay Ravenshaw", 5)

    assert any("Ravenshaw" in h.fact.text for h in hits)


async def test_reseeding_after_delete_replaces_the_user(db: Database) -> None:
    await seed_ram(db)

    assert await delete_user(db, "ram.mohapatra@example.com")
    assert not await delete_user(db, "ram.mohapatra@example.com")
    second = await seed_ram(db)

    assert len(await SqliteMemoryStore(db, FakeEmbedder()).active(second.id, 50)) == 34


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("married Sabitri Puri 1995", "12 February 1995"),
        ("Cyclone Fani roof", "Fani"),
        ("Super Cyclone relief Jagatsinghpur", "Jagatsinghpur"),
        ("Lingaraj temple Monday", "Lingaraj temple every Monday"),
        ("sister Sujata Puri", "Sujata"),
        ("father headmaster", "headmaster"),
    ],
)
async def test_life_details_are_left_out_of_the_context_but_found_by_search(
    db: Database, query: str, expected: str
) -> None:
    user = await seed_ram(db)
    context = await next_call(db, user)

    assert not any(expected in f.text for f in context.facts)
    hits = await SqliteMemoryStore(db, FakeEmbedder()).search(user.id, query, 5)
    assert any(expected in h.fact.text for h in hits), [h.fact.text for h in hits]
