"""Test accounts from a file, to test calls without living through them.

One format, three ways in and out (examples in `tests/fixtures/`): admins upload a
file in Settings, `baatein seed FILE` loads one from the command line, and admins download any
account as a file (`seed_export.py`) that loads back the same.

Dates are relative ("today+3", "next sunday", `days_ago`), so a seed file works on any day.
Memories go through the memory store, so they are embedded and searchable like real ones.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from baatein.domain.ids import new_id
from baatein.domain.models import (
    Boundary,
    BoundaryScope,
    Certainty,
    DeclinedSuggestion,
    Fact,
    FinalizeStatus,
    MemoryKind,
    OccurrenceStatus,
    Pace,
    Person,
    Preferences,
    Reminder,
    ReminderStatus,
    Repeat,
    Role,
    Sensitivity,
    Session,
    Thread,
    ThreadEvent,
    ThreadKind,
    ThreadStatus,
    Turn,
    User,
    Verbosity,
)
from baatein.memory.service import MemoryDeps, zone
from baatein.voice.personas import PersonaRegistry

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_RELATIVE_DAY = re.compile(r"^today(?:([+-])(\d{1,4}))?$")
_NEXT_WEEKDAY = re.compile(rf"^next ({'|'.join(WEEKDAYS)})$")
# How far into a seeded call its memories were saved, and the gap between its turns.
SAVED_AFTER = timedelta(minutes=5)
TURN_GAP = timedelta(seconds=40)


def resolve_day(value: str, today: date) -> date:
    """ "today", "today+3", "today-2", "next sunday" (after today) or "YYYY-MM-DD"."""
    value = value.strip().lower()
    if match := _RELATIVE_DAY.match(value):
        sign, days = match.groups()
        offset = int(days or 0)
        return today + timedelta(days=-offset if sign == "-" else offset)
    if match := _NEXT_WEEKDAY.match(value):
        ahead = (WEEKDAYS.index(match[1]) - today.weekday()) % 7
        return today + timedelta(days=ahead or 7)
    return date.fromisoformat(value)


def _check_day(value: str | None) -> str | None:
    if value is not None:
        resolve_day(value, date(2000, 1, 1))
    return value


def _check_time(value: str | None) -> str | None:
    if value is not None:
        time.fromisoformat(value)
    return value


class _Seed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SeedUser(_Seed):
    """The account the file creates."""

    email: str
    password: str | None = Field(default=None, min_length=8)
    """Needed for a new account. Left out (as in a download), a replaced account keeps its
    password."""
    display_name: str | None = None
    language: str = "hinglish"
    timezone: str = "Asia/Kolkata"


class SeedPerson(_Seed):
    name: str = Field(min_length=1)
    relation: str | None = None


class SeedTurn(_Seed):
    role: Role
    text: str


class SeedCall(_Seed):
    key: str | None = None
    """Name to refer to this call from other items ("call": "<key>")."""
    days_ago: int = Field(ge=0)
    at: str | None = None
    """Local start time, e.g. "19:00". Default: `minutes` before now, `days_ago` days back."""
    minutes: int = Field(default=10, ge=1)
    summary: str | None = None
    ended_abruptly: bool = False
    unfinished_topic: str | None = None
    notes: list[str] = []
    """"Maybe ask next time" hints, as the end-of-call summary writes them."""
    turns: list[SeedTurn] = []

    _time = field_validator("at")(_check_time)


class SeedMemory(_Seed):
    key: str | None = None
    """Name for `corrects` in a later memory."""
    corrects: str | None = None
    """Key of an earlier memory this one replaces (#12 "Noida nahi, Dwarka")."""
    text: str = Field(min_length=1)
    kind: MemoryKind = MemoryKind.FACT
    quote: str | None = None
    about: str | None = None
    sensitivity: Sensitivity = Sensitivity.NORMAL
    uncertain: bool = False
    importance: int = Field(default=3, ge=1, le=5)
    call: str | None = None


class SeedEvent(_Seed):
    text: str
    status: ThreadStatus | None = None
    call: str | None = None
    days_ago: int | None = Field(default=None, ge=0)
    """When it happened, if not linked to a call. Default: now."""


class SeedThread(_Seed):
    key: str | None = None
    """Name for a boundary's `thread`."""
    title: str = Field(min_length=1)
    kind: ThreadKind = ThreadKind.PLAN
    status: ThreadStatus = ThreadStatus.OPEN
    next_step: str | None = None
    check_in_after: str | None = None
    last_offered_days_ago: int | None = Field(default=None, ge=0)
    times_offered: int | None = Field(default=None, ge=0)
    """Default: 1 if `last_offered_days_ago` is given, else 0."""
    about: str | None = None
    sensitivity: Sensitivity = Sensitivity.NORMAL
    call: str | None = None
    events: list[SeedEvent] = []

    _day = field_validator("check_in_after")(_check_day)


class SeedBoundary(_Seed):
    topic: str = Field(min_length=1)
    scope: Literal["until_reopened", "until_date"] = "until_reopened"
    until: str | None = None
    thread: str | None = None
    """Key of the storyline this topic belongs to."""
    call: str | None = None

    _day = field_validator("until")(_check_day)


class SeedDeclined(_Seed):
    text: str = Field(min_length=1)
    call: str | None = None


class SeedOccurrence(_Seed):
    """One occurrence of a reminder the user already marked done or skipped."""

    date: str
    status: OccurrenceStatus

    _day = field_validator("date")(_check_day)


class SeedReminder(_Seed):
    title: str = Field(min_length=1)
    date: str | None = None
    time: str = "09:00"
    repeat: Repeat = Repeat.ONCE
    days_before: list[int] = [0]
    about: str | None = None
    note: str | None = None
    status: Literal["done", "cancelled"] | None = None
    """Default: active, or waiting for a date when there is none."""
    settled: list[SeedOccurrence] = []
    call: str | None = None

    _day = field_validator("date")(_check_day)
    _time = field_validator("time")(_check_time)


class SeedPreferences(_Seed):
    address_as: str | None = None
    pace: Pace | None = None
    verbosity: Verbosity | None = None
    fewer_questions: bool = False


class SeedFile(_Seed):
    user: SeedUser | None = None
    persona: str | None = None
    preferences: SeedPreferences | None = None
    people: list[SeedPerson] = []
    calls: list[SeedCall] = []
    memories: list[SeedMemory] = []
    threads: list[SeedThread] = []
    boundaries: list[SeedBoundary] = []
    declined_suggestions: list[str | SeedDeclined] = []
    reminders: list[SeedReminder] = []

    @model_validator(mode="after")
    def _check_references(self) -> "SeedFile":
        """Every key a file refers to exists, so a bad file fails before anything is saved."""
        calls = _unique_keys("call", [c.key for c in self.calls])
        threads = _unique_keys("storyline", [t.key for t in self.threads])
        memories: set[str] = set()
        for memory in self.memories:
            if memory.corrects is not None and memory.corrects not in memories:
                raise ValueError(f"'corrects' names unknown memory '{memory.corrects}'")
            if memory.key is not None:
                if memory.key in memories:
                    raise ValueError(f"memory key '{memory.key}' is used twice")
                memories.add(memory.key)
        linked: list[
            SeedMemory | SeedThread | SeedEvent | SeedBoundary | SeedDeclined | SeedReminder
        ] = [
            *self.memories,
            *self.threads,
            *(e for t in self.threads for e in t.events),
            *self.boundaries,
            *(d for d in self.declined_suggestions if isinstance(d, SeedDeclined)),
            *self.reminders,
        ]
        for item in linked:
            if item.call is not None and item.call not in calls:
                raise ValueError(f"unknown call '{item.call}'")
        for boundary in self.boundaries:
            if boundary.thread is not None and boundary.thread not in threads:
                raise ValueError(f"unknown storyline '{boundary.thread}'")
        return self


def _unique_keys(what: str, keys: list[str | None]) -> set[str]:
    named = [key for key in keys if key is not None]
    if len(named) != len(set(named)):
        raise ValueError(f"{what} keys must be unique")
    return set(named)


class SeedError(ValueError):
    pass


def problems(exc: ValidationError) -> str:
    """A file's validation errors on one line, e.g. "memories.0.txt: Extra inputs…"."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
    )


@dataclass(frozen=True)
class SeedResult:
    people: int
    calls: int
    memories: int
    threads: int
    boundaries: int
    declined_suggestions: int
    reminders: int


class Seeder:
    def __init__(self, deps: MemoryDeps, personas: PersonaRegistry) -> None:
        self._deps = deps
        self._personas = personas

    async def create_user(self, seed: SeedFile, password_hash: str) -> User:
        """The account from the file's `user` section, with its persona and preferences."""
        if seed.user is None:
            raise SeedError("this seed file has no 'user' section")
        persona = seed.persona or "casual"
        self._personas.get(persona)
        oldest = max((c.days_ago for c in seed.calls), default=0)
        spec = seed.user
        user = User(
            id=new_id(),
            email=spec.email.strip().lower(),
            password_hash=password_hash,
            created_at=self._deps.clock.now() - timedelta(days=oldest + 1),
            display_name=spec.display_name,
            language=spec.language,
            timezone=spec.timezone,
            persona=persona,
            preferences=_preferences(seed.preferences),
        )
        await self._deps.users.add(user)
        return user

    async def load(self, user: User, seed: SeedFile) -> SeedResult:
        """Add everything in `seed` to the user's account. Existing data is kept."""
        if seed.persona is not None:
            self._personas.get(seed.persona)
        loader = _Loader(self._deps, user, seed)
        await loader.run()
        if seed.preferences is not None:
            await self._deps.users.set_preferences(user.id, _preferences(seed.preferences))
        if seed.persona is not None:
            await self._deps.users.set_persona(user.id, seed.persona)
        return SeedResult(
            people=loader.people_created,
            calls=len(seed.calls),
            memories=len(seed.memories),
            threads=len(seed.threads),
            boundaries=len(seed.boundaries),
            declined_suggestions=len(seed.declined_suggestions),
            reminders=len(seed.reminders),
        )


def _preferences(seed: SeedPreferences | None) -> Preferences:
    return Preferences(**seed.model_dump()) if seed else Preferences()


class _Loader:
    def __init__(self, deps: MemoryDeps, user: User, seed: SeedFile) -> None:
        self._deps = deps
        self._user_id = user.id
        self._seed = seed
        self._tz: tzinfo = zone(user.timezone)
        self._now = deps.clock.now().astimezone(self._tz)
        self._today = self._now.date()
        self._calls: dict[str, Session] = {}
        self._facts: dict[str, Fact] = {}
        self._threads: dict[str, Thread] = {}
        self.people_created = 0

    async def run(self) -> None:
        seed = self._seed
        for person in seed.people:
            await self._person(person.name, person.relation)
        for call in seed.calls:
            await self._call(call)
        for memory in seed.memories:
            await self._memory(memory)
        for thread in seed.threads:
            await self._thread(thread)
        for boundary in seed.boundaries:
            await self._boundary(boundary)
        for declined in seed.declined_suggestions:
            item = SeedDeclined(text=declined) if isinstance(declined, str) else declined
            await self._deps.suggestions.add(
                DeclinedSuggestion(
                    new_id(),
                    self._user_id,
                    item.text,
                    self._saved_at(item.call),
                    self._session_id(item.call),
                )
            )
        for reminder in seed.reminders:
            await self._reminder(reminder)

    def _day(self, value: str) -> date:
        return resolve_day(value, self._today)

    def _midnight(self, value: str | None) -> datetime | None:
        return datetime.combine(self._day(value), time(0), self._tz) if value else None

    def _session(self, key: str | None) -> Session | None:
        return self._calls[key] if key is not None else None

    def _session_id(self, key: str | None) -> str | None:
        session = self._session(key)
        return session.id if session else None

    def _saved_at(self, key: str | None) -> datetime:
        """When something from that call was saved: a few minutes in, else now."""
        session = self._session(key)
        return session.started_at + SAVED_AFTER if session else self._now

    async def _person(self, name: str, relation: str | None = None) -> Person:
        people = self._deps.people
        found = await people.find_by_name(self._user_id, name)
        if found is not None:
            if relation and found.relation != relation:
                found = Person(found.id, found.user_id, found.name, found.created_at, relation)
                await people.update(found)
            return found
        person = Person(new_id(), self._user_id, name.strip(), self._now, relation)
        await people.add(person)
        self.people_created += 1
        return person

    async def _person_id(self, name: str | None) -> str | None:
        return (await self._person(name)).id if name else None

    async def _call(self, call: SeedCall) -> None:
        day = self._today - timedelta(days=call.days_ago)
        ends_by_now = self._now - timedelta(minutes=call.minutes)
        if call.at:
            # A call "today at 19:00" loaded in the morning would be in the future: it ends now.
            started = min(datetime.combine(day, time.fromisoformat(call.at), self._tz), ends_by_now)
        else:
            started = ends_by_now - timedelta(days=call.days_ago)
        session = Session(
            id=new_id(),
            user_id=self._user_id,
            started_at=started,
            room_name="seed",
            ended_at=started + timedelta(minutes=call.minutes),
            end_reason="seed",
            summary=call.summary,
            finalize_status=FinalizeStatus.DONE,
            memory_synced_seq=len(call.turns),
            ended_abruptly=call.ended_abruptly,
            unfinished_topic=call.unfinished_topic,
            notes=tuple(call.notes),
        )
        await self._deps.sessions.add(session)
        if call.key:
            self._calls[call.key] = session
        for seq, turn in enumerate(call.turns, start=1):
            await self._deps.turns.append(
                Turn(session.id, seq, turn.role, turn.text, started + TURN_GAP * seq)
            )

    async def _memory(self, memory: SeedMemory) -> None:
        fact = Fact(
            id=new_id(),
            user_id=self._user_id,
            text=memory.text,
            created_at=self._saved_at(memory.call),
            importance=memory.importance,
            source_session_id=self._session_id(memory.call),
            kind=memory.kind,
            quote=memory.quote,
            about_person_id=await self._person_id(memory.about),
            certainty=Certainty.UNCERTAIN if memory.uncertain else Certainty.SURE,
            sensitivity=memory.sensitivity,
        )
        if memory.corrects:
            await self._deps.store.supersede(self._facts[memory.corrects].id, fact)
        else:
            await self._deps.store.add(fact)
        if memory.key:
            self._facts[memory.key] = fact

    async def _thread(self, seeded: SeedThread) -> None:
        offered = seeded.last_offered_days_ago
        thread = Thread(
            id=new_id(),
            user_id=self._user_id,
            title=seeded.title,
            created_at=self._saved_at(seeded.call),
            kind=seeded.kind,
            status=seeded.status,
            next_step=seeded.next_step,
            check_in_after=self._midnight(seeded.check_in_after),
            last_offered_at=self._now - timedelta(days=offered) if offered is not None else None,
            times_offered=(
                seeded.times_offered
                if seeded.times_offered is not None
                else int(offered is not None)
            ),
            sensitivity=seeded.sensitivity,
            about_person_id=await self._person_id(seeded.about),
            source_session_id=self._session_id(seeded.call),
        )
        await self._deps.threads.add(thread)
        if seeded.key:
            self._threads[seeded.key] = thread
        for event in seeded.events:
            if event.call:
                at = self._saved_at(event.call)
            else:
                at = self._now - timedelta(days=event.days_ago or 0)
            await self._deps.threads.add_event(
                ThreadEvent(
                    new_id(),
                    thread.id,
                    event.text,
                    at,
                    event.status,
                    self._session_id(event.call) or thread.source_session_id,
                )
            )

    async def _boundary(self, boundary: SeedBoundary) -> None:
        until = self._midnight(boundary.until)
        await self._deps.boundaries.add(
            Boundary(
                id=new_id(),
                user_id=self._user_id,
                topic=boundary.topic,
                created_at=self._saved_at(boundary.call),
                scope=BoundaryScope(boundary.scope) if until else BoundaryScope.UNTIL_REOPENED,
                session_id=self._session_id(boundary.call),
                until=until,
                thread_id=self._threads[boundary.thread].id if boundary.thread else None,
            )
        )

    async def _reminder(self, reminder: SeedReminder) -> None:
        on = self._day(reminder.date) if reminder.date else None
        if reminder.status is not None:
            status = ReminderStatus(reminder.status)
        else:
            status = ReminderStatus.ACTIVE if on else ReminderStatus.NEEDS_INFO
        saved = Reminder(
            id=new_id(),
            user_id=self._user_id,
            title=reminder.title,
            created_at=self._saved_at(reminder.call),
            on=on,
            at=time.fromisoformat(reminder.time),
            repeat=reminder.repeat,
            alert_days_before=tuple(sorted(set(reminder.days_before), reverse=True)),
            status=status,
            note=reminder.note,
            about_person_id=await self._person_id(reminder.about),
            source_session_id=self._session_id(reminder.call),
        )
        await self._deps.reminders.add(saved)
        for occurrence in reminder.settled:
            await self._deps.reminders.settle(
                saved.id, self._day(occurrence.date), occurrence.status, self._now
            )
