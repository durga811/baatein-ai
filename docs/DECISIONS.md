# Decision log

The decisions taken so far, the options considered, and why. Details live in the linked documents; this page is the index of *why*. Newest decisions are at the end.

**Working rule (applies to every entry):** fix a problem at the layer that causes it (data, model, code structure) and measure the options, rather than adding prompt instructions to cover it up. Prompt text is for behaviour that really is the model's choice.

## Foundations

### D1. Our own memory store on SQLite, not Mem0
- **Options:** Mem0 (open source or platform), Zep, our own store.
- **Chosen:** our own `MemoryStore` on SQLite: a `facts` table, `sqlite-vec` and FTS5, fused with reciprocal rank fusion.
- **Why:** Mem0 has no SQLite vector store (extra infrastructure, no Docker here), costs two extra LLM calls per add, needs a hack for custom embedders, and doesn't cover storylines, reminders, summaries or transcripts. The port stays provider-neutral, so Mem0 or Zep can still be plugged in.

### D2. Claude Haiku for conversation and call summaries
- **Chosen:** `claude-haiku-5-5` for both; Sonnet via env if needed.
- **Why:** first-word latency matters most in voice. Claude 5.5 models reject `temperature`, so it is unset.

## Memory behaviour (`MEMORY_DESIGN.md` §5–7)

### D3. Strict consent
- **Options:** save silently from the transcript, or save only with agreement.
- **Chosen:** saved only with the user's agreement in the call. An explicit "yaad rakhna" is the agreement and saves at once; otherwise Baatein asks first ("Kya main yeh yaad rakhun?").
- **Why:** the scenario library (#1, #10, #34) and trust. The end-of-call analyzer never creates memories.

### D4. Transcripts are an internal log
- **Chosen:** Claude never searches raw transcripts; only summaries reach the next call (the last call's; since D20 also the one before a trivial call, and since D27 other calls from the same day).
- **Why:** transcripts include things the user never agreed to keep.

### D5. Reminders are delivered in conversation only, for now
- **Chosen:** reminders are dated objects, brought up in the next call. Baatein never promises a day or time it can't keep (`honest_promise`).
- **Why:** there is no scheduler or outbound call yet (a later phase).

### D6. Persona is a per-user setting
- **Options:** keep the casual persona, switch everyone to the elder persona, or make it per user.
- **Chosen:** per user. Respect words come from the language profile (`honorific`, `formal_you`), not the persona.
- **Why:** each language marks respect differently: "ji" in Hindi, "avare" in Kannada, and in Odia only the formal "you", because its suffixes depend on gender.

### D7. Deletion writes a tombstone; destructive actions take two steps
- **Options:** hard delete, soft delete (hide), or tombstone.
- **Chosen:** tombstone. Wipe the text, quote, vectors and FTS entries; keep a row with no content and `deleted_at`. `forget`, `forget_thread` and `cancel_reminder` act only after the user confirms on their next message.
- **Why:** the deletion is provable (#13, #14), and one misheard sentence can't erase a memory.

### D8. Call summaries name only what was saved
- **Chosen:**
  - One structured Claude call per finished call (`memory/finalizer.py`). The summary names only items saved in that call, skips sensitive ones, keeps active boundaries out, and has at most two notes.
  - "Cut off" counts only if the user spoke last.
  - Paused calls get a fixed summary with no LLM call.
  - Failed summaries are retried from the stored transcript with backoff (1, 2, 4… minutes, up to an hour, 5 attempts).
- **Why:** early versions leaked unsaved details ("son's new job in Pune") and flagged too many calls as cut off.

## Fixes made in structure, not prompts (scenario evals, slice 6)

### D9. Recalled memories stay in the call history
- **Problem:** memories added only to the per-turn context were gone by the next turn. Claude saw its own answer without the memory behind it, and took back correct facts.
- **Chosen:** each recalled memory is also written into the history, once per call (`Assistant._add_note`).

### D10. Gaps the scenarios found, fixed in code
- **The reminder wasn't in context:** `later_reminders` added to the call-start context.
- **The check-in was asked twice:** a code-side "already asked" note.
- **Reminder promises like "9 baje":** `honest_promise` in the tool reply.
- **Why:** each was a missing fact or state, not a wording problem.

## Retrieval (`RETRIEVAL_BENCHMARK.md`)

### D11. Embedding model: Pinecone `llama-text-embed-v2`, 2048 dimensions
- **Options measured:**
  - Local: MiniLM (the old default), `multilingual-e5-large`, `embeddinggemma`, mpnet, Qwen3-0.6B, potion.
  - Local rerankers: bge-v2-m3, jina-v2.
  - Hosted on Pinecone: llama at 384, 1024 and 2048 dimensions, e5-large, bge-reranker, pinecone-rerank-v0. Cohere returned 403.
- **Chosen:** llama-2048.
- **Why:** it's the strongest for English, Hindi and Hinglish, the three languages in focus. On Ram's account through the real search: English 30/31, Hindi 29/31, Hinglish 26/31 found. It also loads no model into the worker, which removes the 1.1 GB per-process warning.
- **Cost:** about 450 ms per search over the network (see D14), and a little Pinecone usage per turn.
- **Rejected:**
  - Local rerankers: near-perfect top result, but 260–330 ms.
  - e5-large: best for Odia and Kannada, but 1.7 GB per process.

### D12. Similarity cut-offs per model and language
- **Chosen:** `config.py:MEASURED_CUT_OFFS`:
  - llama: English 0.20, Hinglish users 0.14 (Sarvam writes their speech in Devanagari), default and `recall` keywords 0.12.
  - MiniLM: 0.3.
- **Why:** llama's right answers score 0.14–0.30, so one shared cut-off of 0.3 dropped most of them.

### D13. English, Hindi and Hinglish first; Odia and Kannada later
- **Chosen:** Odia and Kannada stay in the code but are hidden from signup.
  - Odia speaks through Sarvam `bulbul:v3`, because Cartesia rejects Odia on every model.
  - Kannada speaks through Cartesia `sonic-3`.
- **Why:** with llama their retrieval is weaker (Odia 15/24, Kannada 17/25 first). Revisit with a model that suits them, and have native speakers check the benchmark sentences.

## Latency and call flow (2026-10-30)

### D14. Don't hold the reply for memory search; say "Hmm…" only during a real lookup
**Problem:**
- In the first live call on Pinecone, every per-turn search timed out ("memory recall timed out after 0.30s"), so replies went out without searched memories.
- Starting the search on Sarvam's final transcript gained only about 25 ms, because the turn commits right after it.
- Pinecone took p50 453 ms and p90 665 ms (15 searches).

**Options considered:**

| Option | Verdict |
|---|---|
| Wait longer (about 0.7 s) | Adds about half a second of silence to every reply |
| A filler on every slow turn ("hmm, yaad kar rahi hoon…") | The search is slow on nearly every turn, so nearly every reply would start with a filler, even after "theek hai". "Haan, yaad aaya" before the result is back can be untrue if nothing is found. |
| A filler written for the turn by Claude | Writing it needs an LLM call, which takes about as long as the search |
| Claude says its own lead-in before calling `recall` | Depends on prompt wording and is harder to test; the plain "Hmm…" was preferred |
| A local model only for per-turn search | A second index, and the per-process memory comes back |
| Keyword (FTS) matches straight away | Facts are stored in English but Hinglish speech arrives in Devanagari, so keywords rarely match |
| Get closer to Pinecone's region | Worth doing, but it's a deploy decision |

**Chosen:**
1. **The reply doesn't wait for the per-turn search** (`recall_timeout_seconds` = 0). Pinecone never answers within a short wait, so any wait is silence for nothing; a search that has already finished is still used. With a local embedder (5–35 ms), set it to 0.1 s. A slower search keeps running, and its memories are added to the call history for the next turn, once each (`Assistant._arrived_late`). Nothing is added in privacy mode.
2. **Memories needed right away come from Claude's `recall` tool.** If that search takes over 0.3 s (`thinking_after_seconds`), Baatein says the language profile's `thinking` line ("Hmm…", about 0.7 s of audio) through `session.say(..., add_to_chat_ctx=False)`. It stays out of the history, transcript and summary, and the caller can talk over it.

**Why:**
- No added delay on ordinary turns.
- A natural pause instead of silence, and only while a lookup is really happening.
- Nothing claimed before it's known.
- Simple: one phrase per language, no extra LLM call.

**Watch:** these logs, from live calls:
- "the search took N ms"
- "the reply waited N ms"
- "still searching; replying without it"

### D15. The call ends at hang-up
- **Problem:** after a hang-up the LiveKit job lingered until the server closed the empty room (about 20 s). Every call's end reason read "parent process shutdown", and summaries waited.
- **Chosen:** `entrypoint` calls `ctx.shutdown()` from the session's `close` event. The end reason is now e.g. "session closed: participant_disconnected".

## Decision steps in the memory flow (2026-10-30)

An evaluation looked at where a "decision step" (rules, a small classifier, or an LLM call) would make the memory layer more efficient or reliable. It used 252 labelled sentences (live call turns, seeded turns, benchmark questions), the production search path and cached llama vectors. Scripts and results are in the session scratchpad.

### D16. A filler-word list decides whether a turn is searched; no classifier
- **Problem:** every turn went to Pinecone, including "ठीक है" and "haan", and "Yeah, it's good" keyword-matched "felt good afterwards".
- **Options measured (useful hits lost out of 134, searches skipped):**

| Option | Skipped | Useful lost | Time | Verdict |
|---|---|---|---|---|
| Filler words (Roman and Devanagari) | 30% of live turns | 0 | ~0 | **chosen** |
| Character n-gram classifier | 16% | 2 | 0.1 ms | no better than the word list |
| MiniLM classifier (local) | 13–18% | 0–4 | 7 ms | +800 MB per worker |
| Haiku decides | most small talk | 0 | **1.0 s typical, 3.4 s worst** | slower than the 450 ms search it avoids |
| Stricter cut-off on results | 0 | 5 | 0 | loses about one answer per noise case removed |
| Drop the per-turn search | 100% | 23 passing mentions | 0 | Claude can't know a memory exists unless it sees it |
| A classifier on the llama vector we already have, deciding whether to *use* results | — | — | µs | Looked good in mixed cross-validation (12/19 noise removed, 0 lost). But trained without live calls, it removed 0/7 live noise without losing live hits: too little real data. Revisit once many real calls are logged. |

- **Chosen:** `text.py:FILLERS`. A turn made only of fillers has no search terms and isn't searched. Keyword queries ignore those words too.
- **Result on the production path:** useful hits 134 → 134; small talk returning memories 19 → 9; live turns sent to Pinecone 80 → 55.

### D17. A keyword match needs a little shared meaning
- **Problem:** keyword matches skipped the similarity cut-off, so a shared word alone ("sleep", "pine cone") pulled up an unrelated memory at similarity 0.00–0.08.
- **Chosen:** keep a keyword match only if its similarity is at least 0.10 (`ranking.py:KEYWORD_FLOOR`). Every keyword match that was the right answer scored 0.13 or more. Requiring the full cut-off instead would have lost 5 right answers. A fact without a vector yet (embedder switched, `reembed` not run) keeps its keyword match.

### D18. The per-turn search starts when the turn ends
- **Why:** starting it on each final transcript gained about 25 ms, and each extra transcript segment cost its own Pinecone call. With the reply no longer waiting for it (D14), the early start bought nothing.

### D19. Don't save a near copy; ask Claude instead
- **Problem:** a second "yaad rakhna" for the same thing saved a near copy, and Durga's account had both "Ramesh" and "Ramesh (college friend)".
- **Measured (llama, Ram's account, 11 cases):** rephrased duplicates and updates scored 0.83–0.98 against the saved fact; different but related facts scored at most 0.74.
- **Chosen:**
  - `remember` checks for saved memories at or above the model's measured threshold (`config.py:MEASURED_DUPLICATES`, llama 0.80). It reuses the vector it embeds anyway, so there's no extra API call. If one is found, nothing is saved, and Claude is shown the memory and told to say it already remembers, call `correct_memory` if a detail changed, or save again with `different=true` if it really is something else. The user still decides; consent rules hold.
  - Models with no measured threshold skip the check.
  - `person()` moves a relation written into the name ("Ramesh (college friend)") into the relation.
- **Checked on real Pinecone vectors:** a repeat (0.98) and an update (0.93) were caught; a related new fact was saved.

### D20. Trivial calls get no summary, and the next call looks one call further back
- **Problem:** a call where Ram said only "बढ़िया" and "ठीक" got "A brief, light check-in call…", which then opened the next call in place of a real summary (3 of 25 calls were like this).
- **Chosen:**
  - A call where the user said at most 3 search words (fillers don't count) and saved nothing gets no summary and no LLM call (`finalizer.py:TRIVIAL_WORDS`).
  - When the last call has no summary, the call-start context adds the most recent earlier call that has one ("It was very brief. The call before it…"). Reminder timing still counts from the real last call.

### Later
- **Wait for the search when a saved person or relation is named** ("मेरे पिताजी…"), with "Hmm…": about +450 ms on roughly 15% of turns. Try it in live calls before deciding.
- **A clear "yes" check before confirming forget or cancel,** because garbled speech recognition does happen ("साहिब क्यों?" for "save").
- **Not worth it now:** reranking `recall` results (+500 ms), rewriting queries with an LLM (at most one more hit), choosing the cut-off from the turn's script.

## Phase 6 (2026-10-30)

### D21. "Forget me" erases what Baatein remembers and keeps the account
- **Options:** delete the account and everything; erase memories and keep the account; offer both.
- **Chosen (by the user):** erase memories, keep the account and its settings (language, persona, how they like to talk).
  - Facts become tombstones, as when forgetting one (D7): text, quote and person link wiped, row kept with `deleted_at`.
  - People, storylines and their events, boundaries, declined suggestions, reminders, and every call with its transcript and summary are deleted.
  - Vectors are dropped from **every** model's index. Single-memory `forget` used to leave them in an older embedder's index; it no longer does.
- **Where:**
  - the web app (the password is asked again, so a borrowed, logged-in phone can't wipe memories): `POST /me/forget`
  - `baatein forget-user EMAIL --yes`
  - **Not by voice:** asked to forget everything, Baatein points to the app and offers to forget one named thing now. A misheard sentence shouldn't be able to wipe everything.
- **During a call:** the web client blocks erasing until the call ends. If a call's session is erased anyway (for example from the CLI), the call ends quietly; turns or saves after the erase fail and are logged.

### D22. Each call keeps its numbers; `baatein metrics` reports them
- **Why:** tuning choices (how long to wait for a search, whether to add a "wait when a person is named" step, whether a classifier is worth it) should use real calls, not guesses.
- **Options:** structured logs only; per-call metrics with a CLI report; the same plus a dashboard. **Chosen (by the user):** per-call metrics plus a CLI report.
- **What's kept per call** (`baatein/metrics.py:CallMetrics`, JSON in `sessions.metrics`, migration 0008):
  - **Reply latency:** when the caller stops speaking to when Baatein starts replying (LiveKit's per-message `e2e_latency`), with its parts: transcript, end of turn, Claude's first token, TTS first audio. Also interrupted replies.
  - **Memory:** searches sent and skipped (fillers), search time, memories that reached their reply against those that joined the next turn, `recall` tool calls, "Hmm…" said, saves and saves turned down as already saved.
  - **Usage:** Claude tokens (in, cached, out), speech-to-text minutes, TTS characters.
- **Saved when the call ends;** a failure to save is logged and never fails the call's end.

### D23. A demo clock, so one recording session can show several days
- **Why:** the demo shows Baatein reminding the day before an event and following up after it, which needs calls on different days.
- **Chosen:** `DemoClock` (`BAATEIN_DEV__CLOCK_FILE`, development only). `baatein demo-date 2026-11-02 --time 10:00` sets the moment, and time runs on from it. Memory, reminders, the seeder and summaries use it; logins and LiveKit tokens keep the real clock, because the LiveKit server checks real time. The file is read on every call, so the worker, API and CLI follow a change without restarting.

### D24. A language's voice can be swapped from settings, without changing the default
- **Why:** the demo should use Sarvam's most expressive voices (`bulbul:v4-flash`: "companion", "conversational" and code-mixed `*_enhi_*` styles), while the product keeps the voice chosen for it. Hinglish uses Cartesia "Sneha", which reads Baatein's Roman-script Hinglish well.
- **Chosen:**
  - `BAATEIN_VOICE__TTS` (`VoiceSettings.tts`) replaces a profile's text-to-speech and its pace option, by language code.
  - An override for a language with no profile is refused at startup. A voice name the plugin doesn't know fails when the pipeline is built.
- **Before making a Sarvam voice the Hinglish default:** measure time to first audio and how well it reads Roman-script Hinglish (round-trip through speech-to-text) against Cartesia.

### D25. Review fixes before the demo (2026-10-30)
An independent review of D14–D24 found these, all fixed:
- **A failing per-turn search was silent.** With replies no longer waiting, every search finished "late", and its error was swallowed and counted as a normal search time. It is now logged at ERROR where the search finishes, and counted as `search_failures` in the metrics.
- **"It was very brief"** was said of any last call without a summary, including one whose summary wasn't written yet. Now that wording is used only when the summary was written and came out empty.
- **`baatein metrics`** dated its window by the real clock while demo calls use the demo clock. It now uses the memory clock.
- **Demo clock:** a moment without a time zone is rejected, the warning is logged once, and a warning at startup says the demo clock is on.
- **Web client:** erasing is blocked during a call; the mute label resets; a hang-up while connecting no longer leaves the timer running; an expired login no longer reads as a wrong password.
- **Erasing is logged** (`erased everything Baatein remembered about user <id>`).

### D26. A reminder that is due is brought up in the greeting (2026-10-30)
Found in a live call: Ram asked to be reminded "in five minutes" that he had to go out. Baatein said honestly that it can't call him later (it never calls out; D5) and saved a reminder for today. In his next call, a minute later, it said nothing until he asked. Two causes:
- **An alert that had already passed when the reminder was made never fired.** A reminder for today with no time alerts at 9 am, which was hours before he asked. An alert counts if it falls between the last call's start and now, so this one never did. Now an alert that had passed when the reminder was made counts as falling when it was made, so the next call brings it up. The same applies to "remind me a day before" when that day is today.
- **The greeting couldn't mention it.** The opening reply is generated with a fixed instruction ("greet them and ask how they are"), so a reminder due now waited, and Claude kept making small talk. The real-Claude reminder test (#34) showed the same thing on Ravi's birthday. The code already knows which reminders are due, so the greeting instruction now adds "then, in the same reply, gently remind them of: …". This uses no extra model call; the reminder is only added to the instruction.

The same test also exposed a bug from D23: the demo clock wrapped a new system clock instead of the app's own clock, so any run with `BAATEIN_DEV__CLOCK_FILE` set (the real-Claude tests load `.env`) ignored the test's clock. It now wraps the clock it is given.

Still not done, on purpose: reminders at a set time ("in five minutes", "at 6 pm"). That needs Baatein to call or notify the user, which is out of scope until there's a phone or push channel.

### D27. The greeting fits the moment (2026-10-30)
Every call opened the same way ("Namaste, Ram Babu! Aap kaise hain?"), even a second call a few minutes after the first, where it asked again how his day was. The greeting instruction is now chosen in code (`opening()`), because the code knows when the last call was:
- **A second call on the same day** doesn't greet as if for the first time or ask again how they are. Baatein welcomes them back and picks up something from that call (its summary is in the context), or asks what's on their mind. The context now gives that call's clock time and how long ago it was ("earlier today (at 4:04 pm, 19 minutes ago)"), not just the day.
- **Other calls** greet as before, fitted to the time of day: morning, afternoon, evening or night.
- **A reminder due now** is still added to the greeting (D26).

This uses no extra model call. It's checked by a real-Claude scenario: a call about dinner at Lipsa's, then one three hours later that brings up the dinner and doesn't ask how his day is going.

Two gaps found in Ram's data, also fixed:
- **A silent call counted as "the last call".** A 37-second call where he said nothing pushed the real conversation's summary back a slot, and moved the start of the reminder window. The context now reads `recent_conversations`, which skips a finished call with no summary and no words from the user. Open calls still count, and so do seeded calls, which have a summary but no transcript.
- **Only the last call's summary was shown.** A third call the same day didn't know what the first was about. The context now also lists every other summarised call from earlier today, oldest first ("Earlier today you also talked: - at 4:01 pm: …"). There are at most five, from the same query.

The web client also got its own fonts: Baloo 2 for the name and headings, and Mukta for text, both from the Indian Type Foundry. They're warm and readable, with Devanagari as well as Latin, and fall back to system fonts until loaded.

### D28. Promise only what's saved; cache the prompt; logs worth reading (2026-10-30)
Three problems from Ram's live calls at 4:45 and 4:47 pm:
- **A promise with nothing behind it.** Asked to "note down" ten pushups for later, Baatein said "main pooch lungi", and in a test even "maine note kar liya", but called no tool. The follow-up call asked about it only because that day's summary happened to be in the context. The memory rules now say: never claim to have noted something, or promise to ask or remind later, unless a tool saved it in this call. "Note this for later" becomes a reminder (today's date for later today) or a storyline, and otherwise Baatein promises nothing. The new real-Claude test, `test_saves_what_it_promises_to_follow_up_on`, failed 2 of 2 before the change and passed 9 of 10 after. The test now also accepts an honest no-save.
- **No prompt caching.** Every reply re-sent the tools, the instructions and the conversation (about 13.5k tokens) at full price. Caching (`anthropic_llm`, `caching="ephemeral"`) marks the tools, the instructions and the last turns. The instructions stay the same for the whole call, because memory notes are added as messages, so the prefix matches. Measured over 4 text turns, from the second reply on 98% of the input was read from cache (8,537 of 8,695 tokens), at about a tenth of the price. Time to first token didn't change measurably (720–1080 ms either way). The cache lives 5 minutes, refreshed on each read, so it covers one call; it isn't shared between users, because each call's instructions hold that user's memories.
- **Logs too noisy to audit.** In dev mode the worker logged at DEBUG: 220 SQLite lines, 48 LiveKit and 43 Sarvam lines in one short call, around 6 of Baatein's own, and no tool calls. Now:
  - **Level:** the worker logs at INFO, also in dev (`AgentServer(log_level="INFO")`), and `baatein-agent dev --log-level debug` brings the detail back. Pinecone's per-request line is off.
  - **Baatein's lines:** one at call start (user, language, persona, what the context holds), one per tool call (name, arguments and result, shortened, with codes and long numbers redacted), one per memory search (time, how many found), and one at call end (length, turns, reply and search p50, tools, Claude tokens and the share cached).
  - **Metrics:** tool calls are counted in the call's metrics and in `baatein metrics`.
  - **Usage:** it's read once from `session.usage` at the end, replacing the deprecated `metrics_collected` event and its warning.

### D29. Sarvam bulbul:v4-flash for the voices; Simran for Hinglish (2026-10-30)
- **Asked for:** a voice that sounds like natural conversation, on Sarvam's newest model, instead of Cartesia.
- **Measured** with the same warm Baatein reply in each voice:
  - **First audio, streaming as in a call:** Sarvam about 220–390 ms, Cartesia about 120–340 ms. That's at most about 0.2 s more, next to Claude's 1–3 s.
  - **Pronunciation:** Sarvam's own speech-to-text heard every candidate's words and names back correctly ("आरव", "बर्थडे").
- **Chosen by ear:** the user compared 9 Sarvam and 7 Cartesia clips (`demo/voices/`) and picked **`simran_enhi_companion`**. "enhi" voices are made for code-mixed Hindi-English, which suits Baatein's Roman-script Hinglish.
- **Other languages:** English uses `sanchita_en_companion` and Kannada `chaitra_kn_conversation`, both heard back correctly. Odia stays on `bulbul:v3`, because v4-flash has no Odia voice.
- **Settings:** pace is Sarvam's `pace` (0.5–2.0 on v4-flash; the elder persona's 0.85 fits). Temperature stays at the default 0.6; the livelier 0.9 sample wasn't preferred.
- **Cartesia** stays registered: `BAATEIN_VOICE__TTS` can switch any one language back without a code change, and its key is now optional. This replaces D24's "measure before changing the default".

### D30. Closed signup, admin test accounts, and one Railway service (2026-10-30)
- **Asked for:** a hosted demo on Railway with a Cloudflare domain and LiveKit Cloud; no public signup; three admins who can load test accounts from seed files, list them and download them in the same format; demo seed data loaded at deploy.
- **Signup** is off by default (`BAATEIN_AUTH__SIGNUP_ENABLED`). `/auth/signup` answers 403, and the page shows Sign up only when the server says it's open. Accounts come from admins, or from `baatein create-user`, which asks for the password.
- **Admins** are a list of emails in settings (`BAATEIN_AUTH__ADMIN_EMAILS`), not a database flag. That keeps one source of truth, set as a secret on the deploy so the addresses stay out of the public repo. The server checks it on every admin route.
- **One format, three ways:** the seed file. Admins upload it in Settings, `baatein seed` loads it, and a download writes it.
  - **Round trip:** a download loads back as the same account, so download, replace, download gives identical files. A test does this for every file in `tests/fixtures/`.
  - **New fields:** to make that true, the format gained a storyline `key`, `times_offered`, a boundary's `thread`, and a reminder's `status` and `settled` occurrences.
  - **Checked first:** references (call, memory and storyline keys) are checked when the file is read, so a bad file fails before anything is saved.
- **Replace is atomic.** The new copy is loaded under a placeholder email, then one transaction removes the old account and gives the new one its email. A failure partway (say, Pinecone down) leaves the old account as it was. A replace without a password keeps the old one. Admin accounts can't be loaded from a file, because a file's password would then decide who is admin.
- **Passwords:** the repo is public, so demo files carry no password. Admins type one when loading. A deploy gives seeded accounts `BAATEIN_AUTH__SEED_PASSWORD`, and `--if-missing` means a redeploy never resets them.
- **`/dev/seed` and "Test memories" are gone.** They let any user add test data to their own account; the admin upload replaces them.
- **Deploy:** one Docker image and one Railway service in Singapore, running `baatein serve` and `baatein-agent start` side by side (`deploy/start.sh`), with SQLite on a volume. A volume can't be shared between services, and both processes need the same database. LiveKit Cloud carries the audio, and the agent only connects out.
  - **Config:** `.railway/railway.ts`, Railway's Infrastructure as Code. `railway.json` is deprecated and stops being read on 2026-12-01.
  - **Memory:** LiveKit keeps one warm call process per CPU core by default, about 250 MB each (measured), so 2 GB or more on a many-core machine. The worker now keeps two: the whole container measured about 1 GB with nothing running, and 2 GB leaves headroom for calls.

### D31. Cartesia sonic-3.6 with Siya for Hinglish (2026-10-30)
- **The problem:** Simran (Sarvam `bulbul:v4-flash`) sometimes changed tone mid-reply, as if another voice took over.
- **The cause:** Sarvam's stream synthesises each piece of a reply (50–150 characters) separately, at temperature 0.6, so each piece can come out with its own pitch and energy.
  - In recordings streamed word by word as in a call, one take had a garbled sound between two sentences, and pitch moved by up to 50 Hz between pieces.
  - Lowering the temperature to 0.3 was worse: a 5.7 s silence in one take.
  - Bigger pieces (120/300 characters) didn't slow it down, but didn't clearly help.
  - Of 8 `bulbul:v3` voices, Ritu was the steadiest. Kavya, Neha and Priya paused mid-sentence, and Neha dropped about 70 Hz after her first piece.
- **Cartesia sonic-3.6** (generally available 2026-08-27) is Cartesia's most natural model. It adds Roman-script Hindi, and the LiveKit plugin sends a whole reply as one stream (`continue: true`), so the tone carries across sentences.
  - The same reply came out in 1–3 pieces with pitch within about 15–35 Hz, every word correct.
  - First audio about 300 ms, against Sarvam's 450–650 ms.
  - The user picked **Siya** by ear.
  - Its emotion tags work only in English, so Hinglish gets its built-in expressiveness only.
- **Caveat:** only one take per voice was measured, because the Cartesia account ran out of credits partway (402). The account needs a top-up before calls can use this voice.
- **Other languages:** English and Kannada stay on Sarvam `bulbul:v4-flash`, Odia on `bulbul:v3`. `BAATEIN_VOICE__TTS` can still switch Hinglish back to Simran without a code change. `CARTESIA_API_KEY` is required again. Clips and scripts are in `demo/voices/` (`make_seam_samples.py`, `make_v3_samples.py`, `make_sonic36_samples.py`).

### D32. Back to Sarvam for Hinglish, with Ishita (2026-10-30)
The user chose Sarvam `bulbul:v4-flash` with **`ishita_enhi_companion`** over Cartesia sonic-3.6 Siya (D31), so Baatein needs no Cartesia account, and every language speaks through Sarvam again. `CARTESIA_API_KEY` is optional again. Ishita is a code-mixed companion voice like Simran (D29).

Sarvam's stream still synthesises each piece of a reply separately, so the tone can shift between sentences (measured in D31). If that's noticeable, the levers are:
- **Bigger pieces:** `min_buffer_size`/`max_chunk_length` 120/300 through `BAATEIN_VOICE__TTS` (no slower, in D31's tests).
- **A Cartesia voice:** sonic-3.6, which keeps one stream per reply.

Don't lower the temperature: 0.3 caused a long silence (D31).

### D33. Hinglish on Sarvam bulbul:v3 with Ritu (2026-10-30)
The user chose Sarvam `bulbul:v3`, voice **ritu**, for Hinglish, replacing Ishita on v4-flash (D32).

**From D31's tests:** the same reply was streamed as in a call, two takes per voice.
- **Steadiest:** Ritu was the steadiest of 8 v3 voices. It came out in 2–3 pieces with pitch within about 17 Hz, every word right, and no pauses mid-sentence; the shortest at 16.3 s; first audio 472–742 ms.
- **Quieter:** v3 voices play about 4–6 dB quieter than v4-flash. The plugin sends `loudness` only for v2 and v4-flash, so v3 can't be turned up from Baatein.
- **Same settings:** the elder persona's pace (0.85) is within v3's 0.3–3.0. Temperature stays at Sarvam's default 0.6; don't lower it (D31).

English and Kannada stay on `bulbul:v4-flash`, Odia on `bulbul:v3` (kavya).
