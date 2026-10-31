# Memory design: lessons from the Voice Buddy scenario library

Source: the Voice Buddy scenario library (edition 3, October 2026). It has **45 scenarios**: 9 original multi-session stories (#1–9), 24 behavioural cases (#10–33) and 12 scheduled-reminder cases (#34–45), followed by a memory and behaviour contract (§4) and acceptance checks (§5).

This document records what each scenario teaches about memory, the gaps in Baatein's current memory layer, the proposed structure, and the decisions taken. It is the input for the Phase 5 plan.

> **As built (Phase 5, 2026-10-30):** this is the design written before the build; where they differ, the code and `docs/DECISIONS.md` win. The main differences: `facts`/`Fact` were extended rather than renamed to `memories`, and there is no consent column (with strict consent, `remember` after a "yes" is the only way in); threads use `last_offered_at` / `times_offered`; the reminder tools are `create_reminder`, `update_reminder`, `complete_occurrence`, `skip_occurrence` and `cancel_reminder`; and besides the last call's summary, the context also gets the one before a trivial call (D20) and other calls from the same day (D27). Due reminders are brought up in the greeting itself (D26).

## 1. Headline findings

1. Today's call-start context (user name, today's date, open follow-ups, plus in-call chat history) covers only about a quarter of what the scenarios need.
2. **Every saved personal memory needs explicit consent** ("Kya main yaad rakhun?"). Speaking openly is not permission (#14). The original Phase 5 plan, where an analyzer silently extracts facts after each call, contradicts this.
3. `recall_memory` currently searches raw past transcripts. That can surface details the user never consented to save (#14, #15) or asked to forget (#13).
4. Follow-ups must be **selective**: the script repeats after every scenario that recall should happen "only in an appropriate future conversation, never mechanically at every opening". At most one check-in per call, chosen by relevance, cooldown and sensitivity.
5. Storylines move through states over several calls (want to call Ramesh → called him → maybe meeting). A single follow-up question can't represent that; we need **threads** with status and a timeline.
6. People, corrections, forgetting, topic boundaries, interaction preferences, sensitive categories and reminders all need their own representation.
7. "I'll remember" and "I'll remind you at 9 am" are different promises (§1.6). Timed alerts need a scheduler and a delivery channel; Baatein must never claim a reminder is set without one.

## 2. Scenario-by-scenario lessons

### Original stories (#1–9): one storyline across several calls

| # | Example from the script | What it teaches | What memory needs |
|---|---|---|---|
| 1 | "Kya main yaad rakhun ki aap Ramesh ko miss karte hain?" → "Maine unko phone kar diya!" → "Abhi sirf baat hui" | One storyline that moves: missing him → wanting to call → called → maybe meeting | A **thread** with a status and a timeline of events, not a single follow-up question |
| 2 | Suresh hid the slippers; "nadi wali aur school wali dono alag yaadein hain" | Stories stay separate and exact, never merged | A **story** memory kind holding the user's own words |
| 3 | Meera wants to sing → five minutes → sang for her granddaughter | Encourage without pressure; the outcome is a milestone, not a finished task | Thread outcome; "koi zaroori homework nahi" means no automatic next follow-up |
| 4 | Apology to his brother; "unka response aapke control mein nahi" | Sensitive relationships: follow up only if the user wants to | **Sensitivity** on the thread; revisit only when the user raises it |
| 5 | Kamala's loneliness; "Sunday ko lambi call" | Never share with family; a plan made in the call is a natural check-in date | Thread `check_in_after` = after Sunday |
| 6 | Arun taught maths for 40 years → fractions with his grandson → "agli baar decimals" | Who the user is (a teacher) shapes what Baatein suggests | Long-lasting **identity facts**, separate from short-lived plans |
| 7 | Mother's Diwali pulao; "jo yaad nahi, use andaze se nahi bharte" | Build memory up over calls; never fill gaps by guessing | Story memory that can be added to, with known gaps marked |
| 8 | Lata waters the plants; "Ek din na ho paaye toh?" | A check-in, not policing | Gentle follow-up mode; no pressure to "complete" |
| 9 | Railway engineer → safe toy project with his grandson | Connect past identity to something safe to do now | Identity facts plus a **person** record (grandson) |

### Behavioural cases (#10–33)

| # | Example | What it teaches | What memory needs |
|---|---|---|---|
| 10 | "Bas theek. Subah chai peeke balcony mein baitha tha." | Most calls should save nothing | The analyzer must be able to return "nothing worth saving" |
| 11 | "Aaj painting yaad nahi dilana" → later "Ab painting ke baare mein poochh sakte ho" | A topic boundary isn't deletion; it can be temporary or lasting, and the user can lift it | A **boundary** record: topic, scope (this call / until the user reopens it), memory kept |
| 12 | "Suman meri behen?" "Nahi, padosan… naam Sunita hai" | Correct without arguing; never keep two contradictory versions as current | A `correct_memory` tool using the supersede chain, plus a person record with the right name and relation |
| 13 | "Family wali baat bhool jao… poori tarah" | Forget for real; say "deleted" only once the database confirms it | A `forget` tool that removes the memory and its transcript turns from anything Claude can search |
| 14 | "Maine kab bola ki ise save karna hai?" / "Baat kar sakte hain, save mat karna" | Speaking openly is not permission | A per-call **don't-save mode**; consent status on every memory |
| 15 | "Tumhe mera purana dost yaad hai?" → "saved information nahi hai" | No fake familiarity; be honest that it's the first call | A `first_call` flag in context; recall returns "nothing found" honestly (already works) |
| 16 | "Main ab yoga class nahi jaana chahta… cancelled mark karun?" | Changing your mind is normal, not failure | Thread status `cancelled`; never suggested again |
| 17 | "Almari ke baare mein mat poochhna" → "ek shelf saaf kiya" | "Not done" is fine; no guilt | Thread outcome `not_done` plus a boundary; progress can be logged |
| 18 | "Poti ne pehli baar cycle chalayi!" → "ab mujhse race lagati hai" | Share the joy; don't turn good news into tasks | A **moment** memory linked to a person (granddaughter) |
| 19 | "Aaj meri patni ki barsi hai" … "Aaj bas baat" | Grief: the user can choose "just talk, don't save" | Sensitive dates never raised proactively unless the user opts in |
| 20 | "Mujhe test ki date yaad nahi" → "date saved nahi hai… andaza nahi lagaunga" | Never guess an unsaved date | Events with exact dates; say plainly when something is missing |
| 21 | "Arre, itne sawaal mat poochho" | Change style and remember the preference | **Interaction preferences** (fewer questions) applied every call |
| 22 | "Hindi aur English mix… Is preference ko yaad rakhna" | Language preference can be saved; don't fake unsupported languages (Kannada) | Profile: language and mix (`users.language` exists) |
| 23 | "Dheere bolo, sunai nahi diya" | Speech pace preference | Preference drives **TTS speed** (Sarvam `pace`) and shorter sentences |
| 24 | "Roz nahi, sirf barsaat mein" | Stories change over time; not verified fact | Memory **certainty** plus versioning (supersede) |
| 25 | "Koi phone par bank OTP maang raha tha" | Never store OTPs, PINs or account numbers | A **redaction filter** before saving; only a high-level note ("got a scam call") |
| 26 | "Mujhe kabhi-kabhi chakkar aata hai" | Health: no diagnosis; save only with specific consent and purpose | Sensitivity `health`; never raised proactively |
| 27 | "Main ab safe nahi hoon" | Safety first; keep as little as possible | Sensitivity `crisis`: minimal retention; next call may gently ask "aap surakshit hain?" |
| 28 | "Tum hi kaafi ho" | Don't encourage dependency | Prompt behaviour; no "attachment profile" |
| 29 | "Tumne kal walk ki baat ki thi, main naraz tha" | Don't repeat a rejected suggestion | A **declined suggestions** record |
| 30 | "Chai mein elaichi nahi" → "ab kabhi-kabhi elaichi bhi" | Preferences change | Preference memory plus supersede |
| 31 | "[call ends]" → "Baat yahin ruk gayi thi…" | Offer to continue once, never assume; a cut-off "yes" is not consent | Last session's `ended_abruptly` flag and unfinished topic; unanswered consent is dropped |
| 32 | "Abhi nahi, kamre mein mehmaan hain" | Others may be listening | Per-call **privacy mode**: no personal details spoken unprompted |
| 33 | "Saheli Neena ke ghar mushkil waqt" | A third party's private story isn't the user's | Memory has `about` (user or another person); third-party sensitive detail kept minimal |

### Reminder scenarios (#34–45): dated, scheduled items

| # | Example | What it teaches | What memory needs |
|---|---|---|---|
| 34 | Ravi's birthday 18 Nov; alerts 11 Nov and 18 Nov at 9 am, every year | One event, several alert times, repeating yearly | Reminder with date, local time, repeat rule and alert list |
| 35 | Aman 4 Mar, Neha 21 Aug | Two people, two separate reminders | A person record each, independent reminders |
| 36 | "Shayad June ke aakhri hafte" | Don't guess a date | Reminder status `needs_info` until the date is known |
| 37 | Glasses "agle shanivaar" → "somvaar ko" | Relative dates and rescheduling | Resolve relative dates in the **user's timezone**; update, never duplicate |
| 38 | Fan repair, "parson aayega" | Baatein doesn't book anything | Reminder text is about what the user will do |
| 39 | Doctor Friday 4 pm; alerts 9 am and 2 pm; "reports le jaani hai" | A reminder can carry a preparation note | Note field on the reminder |
| 40 | Optician: check vs repair vs buy | Keep the action precise | Precise reminder title |
| 41 | Medicine refill; "kal stock aayega" | No medical advice | Reschedule; sensitivity `health` |
| 42 | Bill due on the 28th; alerts on the 25th and 27th | Don't assume it got paid | Status per occurrence: `pending → done` |
| 43 | Passbook: planned → postponed → completed | Explicit status changes | A reminder status log |
| 44 | First Sunday each month, satsang; "sirf is baar skip" | Skip one occurrence, keep the series | Monthly "first Sunday" rule; skipping one doesn't cancel the series |
| 45 | Anniversary 12 Dec; "Iss saal shor-sharaba nahi" | Emotional dates need care | Sensitivity on the reminder; tone can change per year |

## 3. Gaps in the current memory layer

| Today (Phase 4) | What the scenarios need |
|---|---|
| User name | Name plus how to address them ("Mohan ji"), timezone, first-call flag |
| Today's date in UTC | Date and time in the user's timezone (IST). `due_at` is currently UTC midnight, which is wrong for "Friday at 9 am". |
| All open follow-ups in context | One or two chosen check-ins, ranked by relevance, cooldown and sensitivity |
| In-call chat history | Plus where the last call left off (ended abruptly, unfinished story) |
| Facts table (not yet filled) | Memories with kind, subject, consent, sensitivity and certainty |
| — | People, threads, boundaries, preferences, declined suggestions, reminders |
| Transcript search in `recall_memory` | Must respect consent and forgetting (see decision 2) |

## 4. Proposed structure

```
users ─┬─ profile        address_as, timezone, language/mix, speech pace,
       │                 verbosity, fewer_questions, humour/greeting   ← always in context
       ├─ people         name, relation, aliases/spelling, notes  (Ravi = son, Sunita = neighbour)
       ├─ memories       kind: fact | story | preference | moment
       │  (extends       about: user | person_id
       │   facts)        text (English) + quote (user's own words), certainty: sure | uncertain
       │                 sensitivity: normal | personal | health | grief | crisis   (consent: implied by remember)
       │                 superseded_by, retired_at, deleted_at
       ├─ threads        kind: relationship | aspiration | intention | worry | plan | story
       │  (replaces      status: open | in_progress | done | not_done | cancelled | snoozed
       │   followups)    next_step, check_in_after, last_offered_at, times_offered, sensitivity
       │                 └─ thread_events  (date, what happened: "called Ramesh", "decided on Sunday")
       ├─ boundaries     topic or thread_id, scope: this_call | until_reopened | until_date
       ├─ suggestions    declined ones (e.g. "go for a walk", #29), so they aren't repeated
       ├─ reminders      title, person_id, event_date, local_time, timezone, recurrence rule,
       │                 alerts[], note, status: needs_info | active | done | cancelled
       │                 └─ reminder_occurrences (status per occurrence)
       └─ sessions       summary (consented items only), ended_abruptly, unfinished_topic
             └─ turns    internal log only (see decision 2)
```

`followups` becomes `threads`, and `facts` is extended with the memory fields (built that way rather than renamed to `memories`). Both are additive migrations; repositories and the `MemoryStore` contract tests carry over.

### Call-start context (small and chosen, not everything)

```
1. Profile:     "Mohan ji · Hindi-English mix · prefers slow, short sentences · fewer questions"
2. Time:        "Thu 29 Oct 2026, 7:40 pm IST" · first call? / last call 2 days ago, ended abruptly mid-story
3. Boundaries:  "Don't bring up: painting (until he reopens it), almari"          ← always included
4. People:      "Ravi (son, birthday 18 Nov) · Sunita (neighbour)"
5. Check-in:    at most ONE thread, picked by ranking:
                due or relevant · not snoozed · not sensitive unless the user raises it · not raised recently
                "Ramesh: he planned to call him (talked 3 days ago). Ask gently, accept 'no'."
6. Reminders:   due in the next 7 days, mentioned in conversation (decision 3)
7. Last call:   its summary (decision 2); as built, also the call before a trivial one and other calls today
8. Rules:       ask before saving · never invent · say "I don't have that saved"
```

On each user turn, search memories and threads, skipping anything without consent, deleted, or under a boundary. Sensitive items are shown only when the user's own words raise them.

### Tools for Claude (replacing `recall_memory`, `add_followup`, `resolve_followup`)

| Tool | Scenarios |
|---|---|
| `remember(kind, text, about, sensitivity)`, only after the user says yes | 1–9, 18, 22, 30 |
| `correct_memory(id, new_text)` | 12, 24, 30 |
| `forget(id or topic)` | 13, 14 |
| `update_thread(id, status, event, next_step, check_in_after)` | 1, 3, 5, 6, 8, 16, 17 |
| `set_boundary(topic, scope)` / `lift_boundary` | 11, 17, 19, 32 |
| `set_preference(...)` | 21, 22, 23, 30 |
| `create_reminder` / `update_reminder` / `complete_occurrence` / `skip_occurrence` / `cancel_reminder`, read back to the user before saving | 34–45 |
| `recall(query)` over consented memories and threads only | all |

### End-of-call analyzer (Phase 5)

- Writes a summary of the call that mentions only consented items, plus `ended_abruptly` and the unfinished topic.
- Applies thread events and status changes the user agreed to during the call.
- **Never creates memories silently.** Something worth keeping that the user wasn't asked about becomes a "maybe ask next time" note, not a memory.
- Runs a redaction filter for OTPs, PINs, account and card numbers on everything it writes.

## 5. Decisions

| # | Question | Decision |
|---|---|---|
| 1 | Consent model | **Strict.** Every personal memory is saved only with the user's agreement in the call. No silent extraction. Refined on 2026-10-29: an explicit request ("Ek baat yaad rakhoge? …", "yaad rakhna") is the agreement, so it is saved right away (#34); when Baatein itself wants to keep something it asks first ("Kya main yaad rakhun?" → "Haan", #1). **Destructive actions are always confirmed**, even right after an explicit request: forget asks "Pakka, main yeh poori tarah mita doon?" and deletes only on the user's next message (#13). |
| 2 | Transcripts | **Internal log only, plus call summaries.** Claude never searches raw transcripts; `recall` covers consented memories and threads. The last call's summary goes into call-start context (as built, also the one before a trivial call and other calls from the same day: D20, D27). |
| 3 | Reminder delivery | **In the next conversation only, for now.** Reminders are stored as first-class dated objects; Baatein brings due ones up when the user next talks to it and never claims it will "notify" or "call" at a time. Timed delivery (scheduler + outbound call or push) is a later phase. |
| 4 | Audience and persona | **Persona as a per-user setting** (option C in §6). The profile holds a persona (tone, form of address, pace, sentence length, question frequency) next to the language. An elder persona follows the scenario library; the current casual Hinglish persona stays. Preferences from #21–23 adjust it per user. |
| 5 | Deletion | **Tombstone** (option C in §7). `forget` wipes the content (text, quote, vectors, FTS entries, related transcript turns) and keeps a contentless row (`id`, `deleted_at`, requested by the user) so the deletion is provable. Baatein says "deleted" only after the write succeeds. |

## 6. Audience and persona (decided: option C)

The scenario library is written for **older adults** (most characters are 68–78): respectful address with "ji", Hindi-first with some English, patient pacing, short sentences, repetition on request (#23), no assumptions about hearing or memory decline, clear AI identity, and no pretending to be a friend or relative (#28).

Baatein's current prompt is aimed at a younger user: "natural, casual Hinglish the way urban Indians talk", with Roman-script output, a normal TTS pace and up to three sentences.

Options:
- **A. Keep Baatein as is.** Use the library only for its memory lessons.
- **B. Switch Baatein to the elder persona.** Change the default prompt, greeting, pace and address style.
- **C. Make persona a per-user setting** (recommended direction). Add a persona to the profile next to the language: tone, form of address, pace, verbosity, question frequency. The elder persona is one profile; the current casual one stays. Preferences from scenarios #21–23 adjust it per user, and the demo can run the elder persona.

## 7. Deletion (decided: option C, tombstone)

What the document says:
- #13 ("Forget this completely"): "Delete or suppress the specified saved memory per product policy." Boundary: "Distinguish deletion of saved memory from any separate operational retention obligations." Baatein says it will process the request and confirm only once done; in the next call it does not use the deleted detail; if the user tells the story again, it listens but does not bring back the old one.
- #14: "confirmation ke bina deleted nahi bolunga" (it won't say "deleted" without confirmation), and if the status can't be verified it doesn't claim certainty.
- #11: a "not today" boundary is **not** permission to delete.
- §4 and §5: correction and deletion must affect later responses, and must be provable from the database.

"Suppress" means hiding a memory but keeping the row. "Operational retention" means records kept for reasons other than memory (logs, backups, audits, legal requirements), which a memory deletion doesn't necessarily remove.

Options:
- **A. Hard delete**: remove the memory row, its vectors and FTS entries, and the related transcript turns. Simplest promise; nothing left to audit.
- **B. Suppress (soft delete)**: mark `deleted_at`, keep the text. Easy to undo, but "forgotten" data still exists.
- **C. Tombstone** (recommended): delete the content (text, quote, vectors, FTS, related transcript turns) but keep a contentless row (`id`, `deleted_at`, who asked) so the deletion is auditable and provable, matching §5's "provable from the database".
