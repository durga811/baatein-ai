# Retrieval benchmark: finding the right memory in English, Hindi, Hinglish, Odia and Kannada

> **Decision (2026-10-30):** Baatein uses Pinecone `llama-text-embed-v2` at 2048 dimensions, focusing on English, Hindi and Hinglish for now. It is the strongest model here for those three (English 29/31 first, AUC 0.98; Hindi 28/31, 0.89; Hinglish 24/31, 0.80). Its round trip (about 450 ms) is too slow to hold each reply for, so per-turn results join the next turn and the `recall` tool says "Hmm…" while it searches (`docs/DECISIONS.md`, D14); similarity cut-offs are set per language from these results. Odia and Kannada stay in the code but off the signup form until their retrieval is better (local `multilingual-e5-large` is the strongest for them).

Baatein looks up saved memories in two ways:
- **Per-turn recall**: on every user turn it searches with the user's own words, as Sarvam transcribes them (`Assistant.on_user_turn_completed`). Results are added as a hidden note; since D14 the reply no longer waits for them, and a slower search's results join the next turn.
- **The `recall` tool**: Claude searches with a few English keywords when it needs something it doesn't have.

Memories are stored as short English sentences. Spoken questions arrive in another language and script, so retrieval has to work across languages. This benchmark measures how well each embedding model, and optionally a reranker, does that. It is the basis for choosing a model rather than patching prompts.

## How to run it

```bash
uv run python -m bench.retrieval.run                    # the default embedding models
uv run python -m bench.retrieval.run --models intfloat/multilingual-e5-large \
  --rerank BAAI/bge-reranker-v2-m3-int8                 # add a reranker over the top 10
```

Models download from Hugging Face through FastEmbed into `data/models`. Each model runs in its own process, so its memory use is its own, and goes through Baatein's real `FastEmbedEmbedder`, so query and passage prefixes match production. Per-model results are written to `bench/retrieval/results/` (not committed).

## The dataset (`bench/retrieval/queries.json`)

- **Corpus:** Ram's 34 current memories from `tests/fixtures/ram_mohapatra.json`, in English.
- **31 questions**, each in five forms:
  - `hindi`: Devanagari with English words in Latin, the way Sarvam's `codemix` STT returns Hindi. This is what per-turn recall searches for a Hinglish user.
  - `hinglish`: the same in Roman script, as typed or as another STT might return it.
  - `odia`: Odia script, as Sarvam returns Odia.
  - `kannada`: Kannada script, as Sarvam returns Kannada.
  - `keywords`: English keywords, as Claude writes them for the `recall` tool.

  28 are direct questions ("Mere Bapa kya karte the?") and 3 are passing mentions ("Aaj Prafulla se phir chess mein jeet gaya!"), which is what per-turn recall exists for.
- **6 small-talk lines** in each spoken form ("Aaj mausam bahut achha hai", "Haan, theek hai"), which should find nothing.

The Odia and Kannada lines were written by Claude and should be reviewed by native speakers.

## What is measured

- **Right memory first / in the top 5:** how often the expected memory ranks first, or within the 5 memories recall returns (`BAATEIN_MEMORY__RECALL_LIMIT`).
- **Questions vs small talk:** per-turn recall must also stay quiet on small talk, or every "haan, theek hai" injects unrelated memories. AUC is how often a right answer scores above small talk's best match (1.0 = always, 0.5 = chance). In brackets: at the cut-off that best separates them, the share of questions still answered, and the share of small talk let through.
- **Cost:** query time on this Mac's CPU (p50 / p95), load time (including the first download), peak memory of the process.

This measures the model alone (dense similarity). Production adds keyword search (FTS5) and a similarity cut-off per model and language (`config.py:MEASURED_CUT_OFFS`, taken from these results).

An `english` form (full English sentences, for English-speaking users) was added on 2026-10-30, after the first runs. The tables below come from the earlier runs and have no English column; the English figures in the decision note above come from the later Pinecone runs.

## Results (2026-10-30, Apple Silicon Mac, CPU)

Models tried: 6 local embedding models and 2 local rerankers (below), then Pinecone's hosted models (see [Hosted models on Pinecone](#hosted-models-on-pinecone-2026-10-30)). Not tried: BGE-M3 as an embedding model (not in FastEmbed), `jina-embeddings-v3` and full-size Qwen3 (about 2.3 GB each), and Pinecone's Cohere rerankers (not enabled for this Pinecone project).

**Right memory first / in the top 5, out of 31**

| Model | Dim | hindi | hinglish | odia | kannada | keywords |
|---|---|---|---|---|---|---|
| MiniLM-L12 (default then) | 384 | 25 / 28 | 9 / 13 | 12 / 21 | 15 / 21 | 28 / 30 |
| multilingual-mpnet-base | 768 | 25 / 29 | 10 / 16 | 19 / 27 | 22 / 27 | 28 / 31 |
| **multilingual-e5-large** | 1024 | 26 / 30 | 16 / 25 | **24 / 28** | **25 / 29** | 28 / 30 |
| embeddinggemma-300m | 768 | **29 / 31** | **20 / 27** | 18 / 25 | 25 / 31 | 29 / 31 |
| Qwen3-Embedding-0.6B (int8) | 1024 | 28 / 31 | 16 / 21 | 26 / 29 | 23 / 27 | 22 / 28 |
| potion-multilingual-128M | 256 | 18 / 26 | 14 / 20 | 12 / 22 | 19 / 23 | 27 / 31 |
| MiniLM + bge-reranker-v2-m3 | | 30 / 30 | 15 / 16 | 26 / 27 | 24 / 24 | 28 / 30 |
| e5-large + bge-reranker-v2-m3 | | 30 / 30 | 19 / 25 | 27 / 29 | 29 / 30 | 29 / 30 |
| e5-large + jina-reranker-v2 | | 29 / 30 | 17 / 26 | 27 / 29 | 28 / 30 | 30 / 30 |

**Questions vs small talk: AUC (answered / small talk let through, at that language's best cut-off)**

| Model | hindi | hinglish | odia | kannada |
|---|---|---|---|---|
| MiniLM-L12 (default then) | 0.91 (74% / 0%) | 0.32 (23% / 0%) | 0.69 (55% / 0%) | 0.54 (48% / 17%) |
| multilingual-mpnet-base | 0.91 (77% / 0%) | 0.42 (23% / 0%) | 0.80 (61% / 0%) | 0.86 (65% / 0%) |
| **multilingual-e5-large** | 0.96 (90% / 0%) | 0.59 (42% / 0%) | **0.87 (81% / 0%)** | **0.94 (90% / 0%)** |
| embeddinggemma-300m | **0.97** (84% / 0%) | **0.70** (58% / 0%) | 0.46 (39% / 0%) | 0.85 (71% / 0%) |
| Qwen3-Embedding-0.6B (int8) | 0.85 (74% / 0%) | 0.48 (32% / 0%) | 0.78 (71% / 0%) | 0.80 (61% / 0%) |
| potion-multilingual-128M | 0.77 (71% / 0%) | 0.53 (39% / 0%) | 0.49 (39% / 0%) | 0.76 (71% / 0%) |
| e5-large + jina-reranker-v2 | 0.93 (90% / 0%) | 0.52 (45% / 0%) | 0.90 (81% / 0%) | 0.96 (94% / 0%) |

**Cost per query and process** (p50 / p95 ms; peak RSS of the benchmark process, which includes Python and onnxruntime)

| Model | Query ms | RSS MB |
|---|---|---|
| MiniLM-L12 (default then) | 4 / 6 | 850 |
| multilingual-mpnet-base | 20 / 34 | 3,738 |
| multilingual-e5-large | 34 / 48 | 1,740 |
| embeddinggemma-300m | 16 / 25 | 970 |
| Qwen3-Embedding-0.6B (int8) | 581 / 1,042 | 3,300 |
| potion-multilingual-128M | 0.1 / 0.2 | 1,555 |
| any embedder + a reranker | 260 to 330 | +1,300 to +2,500 |

### What this says

1. **The current model is the weak point for Odia and Kannada**, not the prompt: 21 of 31 in the top 5, and it can barely tell a Kannada question from small talk (AUC 0.54). It is fine for Hindi.
2. **multilingual-e5-large is the best all-round choice for Baatein's four languages**: top-5 recall of 28 to 30 for the scripts Sarvam returns, the best Odia and Kannada separation, 34 ms a query (well inside the 0.3 s per-turn budget this benchmark assumed), about 1.7 GB.
3. **embeddinggemma-300m is the light alternative**: best for Hindi and Hinglish at half the memory and 16 ms, but weak for Odia (AUC 0.46, no better than chance). Not suitable while Odia is a supported language.
4. **Rerankers rank almost perfectly but cost 260 to 330 ms on CPU**, the whole per-turn budget. They fit the `recall` tool (Claude is already waiting on a tool call), not per-turn recall.
5. **One similarity cut-off can't serve every language.** With e5-large, each language separates cleanly at its own cut-off (0.78 to 0.82), but one cut-off for all lets 25% of small talk through. The cut-off belongs in the language profile. The single 0.3 then used for MiniLM already let some Kannada small talk through. (Cut-offs are now per model and language: `config.py:MEASURED_CUT_OFFS`.)
6. **Hinglish in Roman script is the hardest form for every model** (at best 27 of 31 in the top 5). It only reaches per-turn recall if the user types; Sarvam returns Devanagari.
7. Keyword queries (the `recall` tool) work with every model (28 to 31 of 31).

### Recommendation (from these local runs; superseded)

> Superseded by the decision at the top: Pinecone `llama-text-embed-v2` was chosen (D11), and replies stopped waiting for the search (D14). Kept as the record of what the local runs suggested.

- Switch the default embedder to `intfloat/multilingual-e5-large` (1024 dimensions) and run `baatein reembed`. Nothing else changes: the `Embedder` port and per-model vector tables already support it.
- Move `min_similarity` into each `LanguageProfile`, starting from these cut-offs, and re-measure with a larger set of small talk before trusting them (6 lines per language is too few to tune on).
- Optionally rerank `recall` tool results with `bge-reranker-v2-m3-int8` (0.57 GB).
- For deployment: at 1.7 GB per process, the embedder should run once per machine (a small embedding service, or one process shared by LiveKit jobs) rather than in every call's job process.
- Don't use a hosted embedder for per-turn recall: Pinecone's round trip from here is 380 to 480 ms, more than the whole budget (see below).

## Hosted models on Pinecone (2026-10-30)

Run with `uv run --env-file .env python -m bench.retrieval.run --models pinecone/llama-text-embed-v2:1024` (and `--rerank pinecone/<model>`), using `PINECONE_API_KEY`. Query times include the network round trip from India, as a call would see it.

**Right memory first / in the top 5, out of 31**, with local e5-large for comparison

| Model | hindi | hinglish | odia | kannada | keywords |
|---|---|---|---|---|---|
| local multilingual-e5-large | 26 / 30 | 16 / 25 | 24 / 28 | 25 / 29 | 28 / 30 |
| llama-text-embed-v2, 384 dims | 25 / 31 | 20 / 29 | 12 / 21 | 15 / 21 | 27 / 31 |
| llama-text-embed-v2, 1024 dims | 26 / 31 | 23 / 30 | 14 / 24 | 16 / 25 | 29 / 31 |
| llama-text-embed-v2, 2048 dims | 28 / 31 | **24 / 31** | 15 / 24 | 17 / 25 | 29 / 31 |
| hosted multilingual-e5-large | 25 / 30 | 15 / 21 | 23 / 28 | 24 / 29 | 26 / 31 |
| local e5-large + hosted bge-reranker-v2-m3 | 30 / 30 | 19 / 25 | 29 / 29 | 29 / 30 | 29 / 30 |
| local e5-large + hosted pinecone-rerank-v0 | 28 / 29 | 22 / 28 | 9 / 18 | 13 / 25 | 29 / 30 |

**Questions vs small talk: AUC (answered / small talk let through)**

| Model | hindi | hinglish | odia | kannada |
|---|---|---|---|---|
| local multilingual-e5-large | 0.96 (90% / 0%) | 0.59 (42% / 0%) | 0.87 (81% / 0%) | 0.94 (90% / 0%) |
| llama-text-embed-v2, 384 dims | 0.83 (77% / 17%) | 0.73 (77% / 17%) | 0.41 (29% / 0%) | 0.47 (61% / 50%) |
| llama-text-embed-v2, 1024 dims | 0.83 (94% / 17%) | 0.68 (48% / 0%) | 0.45 (16% / 0%) | 0.58 (81% / 50%) |
| llama-text-embed-v2, 2048 dims | 0.89 (97% / 17%) | **0.80** (77% / 17%) | 0.53 (26% / 0%) | 0.61 (77% / 50%) |
| hosted multilingual-e5-large | 0.92 (77% / 0%) | 0.50 (19% / 0%) | 0.88 (87% / 0%) | 0.91 (87% / 0%) |
| local e5-large + hosted bge-reranker-v2-m3 | 0.94 (87% / 0%) | 0.48 (35% / 0%) | 0.90 (77% / 0%) | 0.94 (84% / 0%) |
| local e5-large + hosted pinecone-rerank-v0 | 0.90 (81% / 0%) | 0.69 (58% / 0%) | 0.56 (16% / 0%) | 0.66 (58% / 0%) |

**Cost:** every hosted call took 380 to 480 ms at the median (590 to 660 ms p95) for embedding, and about 500 ms (1.9 s p95) for a rerank, against 30 ms for local e5-large. A process using hosted models needs about 100 MB instead of 1.7 GB.

**What this adds:**
1. **llama-text-embed-v2 is the best model for Roman Hinglish** (24 of 31 first at 2048 dimensions), and more dimensions help it steadily. It is **weak for Odia and Kannada** (15 to 17 first, and it lets half of Kannada small talk through), so it doesn't fit Baatein's languages.
2. **Hosted e5-large matches local e5-large** in quality; the difference is only where it runs.
3. **Hosted calls are too slow for per-turn recall**: one round trip is longer than the 0.3 s budget. (Baatein later chose Pinecone anyway and stopped holding the reply for the search; see D11 and D14 in `docs/DECISIONS.md`.) They could serve the `recall` tool, where Claude already waits, but local e5-large plus a local reranker does that faster.
4. **pinecone-rerank-v0 is English-centric** (9 of 31 first for Odia); bge-reranker-v2-m3 is the multilingual reranker to use, local or hosted.
5. The Cohere rerankers (`cohere-rerank-3.5`, `cohere-rerank-4-fast`) returned 403 "Project is not authorized to use model"; they need enabling on the Pinecone project.

At the time, the recommendation stood: local `multilingual-e5-large`. It was later superseded: Baatein chose Pinecone `llama-text-embed-v2` for its quality in English, Hindi and Hinglish, and stopped holding replies for the search (D11, D14).

### Caveats

- The dataset is small and from one persona; the Odia and Kannada lines are not yet reviewed by native speakers.
- Timings were taken on one Mac with other work running; treat them as relative.
- `embeddinggemma-300m` failed to load at first because its ONNX weights are an external file reached through the Hugging Face cache's links, which onnxruntime refuses to follow. Replacing the two links in its snapshot's `onnx/` folder with hard links fixed it.
