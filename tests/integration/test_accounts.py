import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest
from injector import Module, provider, singleton

from baatein.accounts import Accounts
from baatein.config import Settings
from baatein.container import build_root
from baatein.memory.seed import SeedFile
from baatein.ports.embedder import Embedder, EmbedKind
from baatein.storage.sqlite.db import Database
from baatein.storage.sqlite.migrate import migrate
from tests.fakes.embedder import FakeEmbedder


class FlakyEmbedder(FakeEmbedder):
    """Works until `fail` is set, like an embedding service that goes down."""

    fail = False

    async def embed(self, texts: Sequence[str], kind: EmbedKind) -> list[list[float]]:
        if self.fail:
            raise ConnectionError("embedding service unavailable")
        return await super().embed(texts, kind)


def test_a_replace_that_fails_halfway_leaves_the_old_account(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, database={"url": f"sqlite:///{tmp_path}/app.db"})
    migrate(settings.database.sqlite_path())
    embedder = FlakyEmbedder()

    class Embedding(Module):
        @singleton
        @provider
        def embedder(self) -> Embedder:
            return embedder

    seed = SeedFile.model_validate(
        {
            "user": {"email": "ram@example.com", "password": "long-enough"},
            "memories": [{"text": "Loves chhena poda"}],
        }
    )

    async def run() -> list[tuple[bool, int]]:
        injector = build_root(Embedding(), settings=settings)
        accounts = injector.get(Accounts)
        try:
            first = await accounts.load(seed)
            embedder.fail = True
            with pytest.raises(ConnectionError):
                await accounts.load(seed, replace=True)
            return [(row.user.id == first.user.id, row.memories) for row in await accounts.list()]
        finally:
            await injector.get(Database).close()

    assert asyncio.run(run()) == [(True, 1)]


SEEDS = sorted((Path(__file__).parents[1] / "fixtures").glob("*.json"))


@pytest.mark.parametrize("path", SEEDS, ids=lambda path: path.stem)
def test_every_seed_file_loads_and_downloads_back_the_same(path: Path, tmp_path: Path) -> None:
    settings = Settings(_env_file=None, database={"url": f"sqlite:///{tmp_path}/app.db"})
    migrate(settings.database.sqlite_path())

    class Embedding(Module):
        @singleton
        @provider
        def embedder(self) -> Embedder:
            return FakeEmbedder()

    seed = SeedFile.model_validate_json(path.read_text(encoding="utf-8"))

    async def round_trip() -> tuple[SeedFile | None, SeedFile | None]:
        injector = build_root(Embedding(), settings=settings)
        accounts = injector.get(Accounts)
        try:
            first = await accounts.load(seed, password="password123")
            downloaded = await accounts.export(first.user.id)
            assert downloaded is not None
            again = await accounts.load(downloaded, replace=True)
            return downloaded, await accounts.export(again.user.id)
        finally:
            await injector.get(Database).close()

    first, second = asyncio.run(round_trip())
    assert second == first
    assert first is not None
    assert first.user is not None
    assert len(first.memories) == len(seed.memories)
    assert len(first.reminders) == len(seed.reminders)


def test_seed_fixtures_carry_no_password() -> None:
    """The repo is public: a password is given when a file is loaded, never kept in it."""
    assert len(SEEDS) >= 2
    for path in SEEDS:
        seed = SeedFile.model_validate_json(path.read_text(encoding="utf-8"))
        assert seed.user is not None
        assert seed.user.password is None
