import argparse
import asyncio
import getpass
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from dotenv import find_dotenv, load_dotenv
from pydantic import ValidationError

from baatein.accounts import Accounts
from baatein.api.app import create_app
from baatein.auth.service import AuthService
from baatein.config import Settings
from baatein.container import build_root
from baatein.domain.errors import BaateinError
from baatein.memory.finalizer import Finalizer
from baatein.memory.seed import SeedError, SeedFile, problems
from baatein.memory.service import MemoryDeps, MemoryService
from baatein.metrics import CallMetrics, report
from baatein.ports.clock import DemoClock
from baatein.ports.repos import SessionRepo, UserRepo
from baatein.storage.sqlite.db import Database
from baatein.storage.sqlite.memory_store import SqliteMemoryStore
from baatein.storage.sqlite.migrate import migrate


async def reembed(settings: Settings) -> str:
    injector = build_root(settings=settings)
    store = injector.get(SqliteMemoryStore)
    try:
        count = await store.rebuild_index()
    finally:
        await injector.get(Database).close()
    return f"re-embedded {count} facts into {store.vec_table}"


async def finalize(settings: Settings, session_id: str | None) -> str:
    """Summarise finished calls now: every one that is due, or one call (again) by id."""
    injector = build_root(settings=settings)
    finalizer = injector.get(Finalizer)
    try:
        if session_id is not None:
            ok = await finalizer.finalize_by_id(session_id)
            return f"summarised {session_id}" if ok else f"summarising {session_id} failed"
        return f"summarised {await finalizer.run_due()} call(s)"
    finally:
        await injector.get(Database).close()


async def forget_user(settings: Settings, email: str) -> str:
    """Erase everything Baatein remembers about a user; the account stays."""
    injector = build_root(settings=settings)
    try:
        user = await injector.get(UserRepo).get_by_email(email)  # type: ignore[type-abstract]
        if user is None:
            return f"no user {email}"
        await injector.get(MemoryService).forget_user(user.id)
        return f"erased everything Baatein remembered about {user.email}; the account stays"
    finally:
        await injector.get(Database).close()


async def show_metrics(settings: Settings, days: float, email: str | None) -> str:
    """Latency, memory and usage numbers of recent calls (saved when each call ends)."""
    injector = build_root(settings=settings)
    try:
        user_id = None
        if email is not None:
            user = await injector.get(UserRepo).get_by_email(email)  # type: ignore[type-abstract]
            if user is None:
                return f"no user {email}"
            user_id = user.id
        # Calls are dated by the memory clock, which is the demo clock when one is set.
        since = injector.get(MemoryDeps).clock.now() - timedelta(days=days)
        sessions: SessionRepo = injector.get(SessionRepo)  # type: ignore[type-abstract]
        calls = await sessions.metrics_since(since, user_id)
    finally:
        await injector.get(Database).close()
    scope = f" for {email}" if email else ""
    return f"Last {days:g} day(s){scope}\n" + report(CallMetrics.from_json(c) for c in calls)


def demo_date(settings: Settings, day: str | None, at: str, tz: str, clear: bool) -> str:
    """Set (or clear) the moment the demo clock starts from."""
    if not settings.dev.clock_file:
        return "Set BAATEIN_DEV__CLOCK_FILE (e.g. data/demo-clock.json) first, then restart."
    clock = DemoClock(Path(settings.dev.clock_file))
    if clear:
        return "demo clock cleared: real time" if clock.clear() else "demo clock was not set"
    if day is None:
        return "Give a day (YYYY-MM-DD), or --clear to go back to real time."
    moment = datetime.combine(date.fromisoformat(day), time.fromisoformat(at), ZoneInfo(tz))
    clock.set(moment)
    return f"demo clock: it is now {moment:%a %d %b %Y, %H:%M} ({tz}), and time runs on"


# What a bad file or a bad name raises: shown as a message, not a traceback.
REFUSED = (BaateinError, SeedError, LookupError)


def read_seed(path: Path) -> SeedFile:
    try:
        return SeedFile.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise SystemExit(f"{path}: {problems(exc)}") from exc


async def load_seeds(
    settings: Settings, paths: Sequence[Path], replace: bool, if_missing: bool
) -> str:
    """Create the account in each file's `user` section and load the rest into it."""
    seeds = [(path, read_seed(path)) for path in paths]
    password = settings.auth.seed_password
    injector = build_root(settings=settings)
    accounts = injector.get(Accounts)
    lines = []
    try:
        for path, seed in seeds:
            if if_missing and seed.user and await accounts.exists(seed.user.email):
                lines.append(f"{path}: {seed.user.email} already exists, left as it is")
                continue
            try:
                loaded = await accounts.load(
                    seed,
                    replace=replace,
                    password=password.get_secret_value() if password else None,
                )
            except REFUSED as exc:
                raise SystemExit(f"{path}: {exc}") from exc
            counts = ", ".join(
                f"{n} {name.replace('_', ' ')}" for name, n in vars(loaded.added).items()
            )
            action = "replaced" if loaded.replaced else "seeded"
            lines.append(f"{action} {loaded.user.email} ({loaded.user.id}): {counts}")
    finally:
        await injector.get(Database).close()
    return "\n".join(lines)


async def create_user(
    settings: Settings, email: str, name: str | None, language: str | None, persona: str | None
) -> str:
    """An account made by hand, e.g. an admin's, while signup is closed."""
    password = getpass.getpass(f"Password for {email}: ")
    if getpass.getpass("Again: ") != password:
        raise SystemExit("the passwords don't match")
    injector = build_root(settings=settings)
    try:
        user = await injector.get(AuthService).signup(email, password, name, language, persona)
    except REFUSED as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        await injector.get(Database).close()
    role = " (admin)" if settings.auth.is_admin(user.email) else ""
    return f"created {user.email}{role}"


def load_env() -> None:
    """Provider keys (Pinecone, Anthropic) are read from the environment by their SDKs, so
    `.env` goes into it, as the agent worker does; variables already set win."""
    load_dotenv(find_dotenv(usecwd=True))


def main(argv: Sequence[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(prog="baatein")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply database migrations")
    commands.add_parser("reembed", help="rebuild the fact vector index for the current embedder")
    seed_cmd = commands.add_parser("seed", help="create test accounts from seed files")
    seed_cmd.add_argument("files", type=Path, nargs="+")
    existing = seed_cmd.add_mutually_exclusive_group()
    existing.add_argument(
        "--replace", action="store_true", help="load an existing account again from scratch"
    )
    existing.add_argument(
        "--if-missing", action="store_true", help="skip accounts that already exist (deploys)"
    )
    user_cmd = commands.add_parser(
        "create-user", help="make an account by hand (admins; signup is usually closed)"
    )
    user_cmd.add_argument("email")
    user_cmd.add_argument("--name")
    user_cmd.add_argument("--language")
    user_cmd.add_argument("--persona")
    finalize_cmd = commands.add_parser(
        "finalize", help="summarise finished calls now (baatein serve also does it)"
    )
    finalize_cmd.add_argument("--session", help="summarise this call again, whatever its state")
    forget_cmd = commands.add_parser(
        "forget-user", help="erase everything Baatein remembers about a user (keeps the account)"
    )
    forget_cmd.add_argument("email")
    forget_cmd.add_argument(
        "--yes", action="store_true", help="confirm; nothing is erased without it"
    )
    metrics_cmd = commands.add_parser(
        "metrics", help="reply latency, memory search and usage of recent calls"
    )
    metrics_cmd.add_argument("--days", type=float, default=7, help="how far back (default 7)")
    metrics_cmd.add_argument("--email", help="only this user's calls")
    demo_cmd = commands.add_parser(
        "demo-date",
        help="for recording demos: make it a chosen day (needs BAATEIN_DEV__CLOCK_FILE)",
    )
    demo_cmd.add_argument("day", nargs="?", help="YYYY-MM-DD; leave out with --clear")
    demo_cmd.add_argument("--time", default="10:00", help="time of day (default 10:00)")
    demo_cmd.add_argument("--tz", default="Asia/Kolkata", help="time zone (default Asia/Kolkata)")
    demo_cmd.add_argument("--clear", action="store_true", help="back to real time")
    serve = commands.add_parser("serve", help="run the HTTP API and web client")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.command == "forget-user" and not args.yes:
        print(f"This erases every memory, call and reminder of {args.email}. Add --yes to do it.")
        return 1
    settings = Settings()
    path = settings.database.sqlite_path()
    version = migrate(path)
    print(f"{path}: schema version {version}")
    if args.command == "reembed":
        print(asyncio.run(reembed(settings)))
    elif args.command == "finalize":
        print(asyncio.run(finalize(settings, args.session)))
    elif args.command == "forget-user":
        print(asyncio.run(forget_user(settings, args.email)))
    elif args.command == "metrics":
        print(asyncio.run(show_metrics(settings, args.days, args.email)))
    elif args.command == "demo-date":
        print(demo_date(settings, args.day, args.time, args.tz, args.clear))
    elif args.command == "seed":
        print(asyncio.run(load_seeds(settings, args.files, args.replace, args.if_missing)))
    elif args.command == "create-user":
        print(
            asyncio.run(create_user(settings, args.email, args.name, args.language, args.persona))
        )
    elif args.command == "serve":
        uvicorn.run(create_app(build_root(settings=settings)), host=args.host, port=args.port)
    return 0
