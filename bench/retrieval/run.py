"""Compare embedding models on Hinglish memory retrieval.

    uv run python -m bench.retrieval.run                      # the default candidates
    uv run python -m bench.retrieval.run --models a,b         # FastEmbed model names
    uv run python -m bench.retrieval.run --models a --rerank r   # a's top 10, re-scored by r
    uv run --env-file .env python -m bench.retrieval.run --models pinecone/llama-text-embed-v2:1024

Each model runs in its own process (so memory use is its own) through Baatein's real
`FastEmbedEmbedder`, with query and passage prefixes as in production. Dense similarity only:
it measures the model, not the keyword half of hybrid search. Results go to
bench/retrieval/results/<model>.json and a table is printed.
"""

import argparse
import asyncio
import json
import resource
import statistics
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder

from baatein.memory.embeddings.fastembed import FastEmbedEmbedder
from bench.retrieval.pinecone import (
    Client,
    PineconeEmbedder,
    PineconeReranker,
    is_hosted,
    parse,
)

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
RESULTS = HERE / "results"
DATA = json.loads((HERE / "queries.json").read_text())
FORMS: list[str] = DATA["forms"]  # hindi, hinglish, odia, kannada, keywords
SPOKEN: list[str] = DATA["spoken"]  # what per-turn recall searches; small talk comes in these
TOP_K = 5  # BAATEIN_MEMORY__RECALL_LIMIT
RERANK_POOL = 10  # embedding matches the reranker re-scores
DEFAULT_RERANKERS = ["BAAI/bge-reranker-v2-m3-int8", "jinaai/jina-reranker-v2-base-multilingual"]
DEFAULT_MODELS = [
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
    "intfloat/multilingual-e5-large",
    "google/embeddinggemma-300m",
    "Qwen/Qwen3-Embedding-0.6B-Q",
    "minishlab/potion-multilingual-128M",
]


def corpus() -> list[str]:
    """Ram's current memories: every seeded memory except the ones a later one corrects."""
    seed = json.loads((ROOT / "tests" / "fixtures" / "ram_mohapatra.json").read_text())
    replaced = {m["corrects"] for m in seed["memories"] if m.get("corrects")}
    return [m["text"] for m in seed["memories"] if m.get("key") not in replaced]


def dimensions(model: str) -> int:
    if is_hosted(model):
        return parse(model)[1] or 1024
    for described in TextEmbedding.list_supported_models():
        if described["model"] == model:
            return int(described["dim"])
    raise SystemExit(f"{model} is not a FastEmbed text model")


@dataclass
class Result:
    model: str
    reranker: str | None
    dim: int
    load_seconds: float
    peak_rss_mb: float
    query_ms_p50: float
    query_ms_p95: float
    ranks: dict[str, list[int | None]]  # per form, rank (1-based) of the expected memory
    expected_scores: dict[str, list[float]]  # per form, similarity of the expected memory
    negative_top: dict[str, list[float]]  # per form, best similarity for small talk


def unit(vectors: list[list[float]]) -> np.ndarray:
    array = np.asarray(vectors, dtype=np.float32)
    return array / np.linalg.norm(array, axis=1, keepdims=True)


async def measure(model: str, reranker: str | None = None) -> Result:
    data = DATA
    memories = corpus()
    cache = str(ROOT / "data/models")
    client = Client() if is_hosted(model) or (reranker and is_hosted(reranker)) else None
    started = time.perf_counter()
    embedder: FastEmbedEmbedder | PineconeEmbedder = (
        PineconeEmbedder(model, client)
        if client and is_hosted(model)
        else FastEmbedEmbedder(model, dimensions(model), cache_dir=cache)
    )
    passages = unit(await embedder.embed(memories, "passage"))
    rerank: Callable[[str, Sequence[str]], Awaitable[list[float]]] | None = None
    if reranker and client and is_hosted(reranker):
        rerank = PineconeReranker(reranker, client).rerank
    elif reranker:
        cross = TextCrossEncoder(reranker, cache_dir=cache)

        async def rerank_locally(query: str, documents: Sequence[str]) -> list[float]:
            return [float(s) for s in cross.rerank(query, list(documents))]

        rerank = rerank_locally

    if rerank is not None:
        await rerank("warm up", memories[:2])
    load_seconds = time.perf_counter() - started

    async def search(text: str) -> tuple[np.ndarray, float]:
        """Scores for every memory; with a reranker, only its pool is scored, the rest -inf."""
        started = time.perf_counter()
        [vector] = await embedder.embed([text], "query")
        scores = passages @ unit([vector])[0]
        if rerank is not None:
            pool = np.argsort(-scores)[:RERANK_POOL]
            reranked = np.full(len(memories), -np.inf, dtype=np.float32)
            reranked[pool] = await rerank(text, [memories[i] for i in pool])
            scores = reranked
        return scores, time.perf_counter() - started

    timings: list[float] = []
    ranks: dict[str, list[int | None]] = {form: [] for form in FORMS}
    expected: dict[str, list[float]] = {form: [] for form in FORMS}
    for question in data["questions"]:
        wanted = [
            i
            for i, text in enumerate(memories)
            if any(piece.lower() in text.lower() for piece in question["match"])
        ]
        assert wanted, f"{question['id']}: no memory matches {question['match']}"
        for form in FORMS:
            scores, elapsed = await search(question[form])
            timings.append(elapsed)
            order = list(np.argsort(-scores))
            best = max(scores[i] for i in wanted)
            rank = min(order.index(i) for i in wanted) + 1 if np.isfinite(best) else None
            ranks[form].append(rank)
            expected[form].append(float(best) if np.isfinite(best) else -1e9)
    negative: dict[str, list[float]] = {form: [] for form in SPOKEN}
    for line in data["negatives"]:
        for form in negative:
            scores, elapsed = await search(line[form])
            timings.append(elapsed)
            negative[form].append(float(scores.max()))

    timings_ms = sorted(t * 1000 for t in timings[3:])  # skip the first, warming queries
    return Result(
        model=model,
        reranker=reranker,
        dim=dimensions(model),
        load_seconds=round(load_seconds, 2),
        peak_rss_mb=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 0),
        query_ms_p50=round(statistics.median(timings_ms), 1),
        query_ms_p95=round(timings_ms[int(len(timings_ms) * 0.95)], 1),
        ranks=ranks,
        expected_scores=expected,
        negative_top=negative,
    )


def auc(positives: list[float], negatives: list[float]) -> float:
    """How often a right memory scores above small talk's best match (1.0 = always)."""
    wins = sum((p > n) + 0.5 * (p == n) for p in positives for n in negatives)
    return wins / (len(positives) * len(negatives))


def best_threshold(result: dict, forms: list[str]) -> tuple[float, float, float]:  # type: ignore[type-arg]
    """The similarity cut-off that best separates found answers (in the top 5) from small talk
    in these forms. Returns (cut-off, share of questions answered, share of small talk let
    through)."""
    found = [
        score
        for form in forms
        for score, rank in zip(result["expected_scores"][form], result["ranks"][form], strict=True)
        if rank is not None and rank <= TOP_K
    ]
    asked = sum(len(result["ranks"][form]) for form in forms)
    noise = [s for form in forms for s in result["negative_top"][form]]
    best = (0.0, -1.0, 0.0, 0.0)
    for cut in sorted(set(found + noise)):
        answered = sum(s >= cut for s in found) / asked
        leaked = sum(s >= cut for s in noise) / len(noise)
        if answered - leaked > best[1]:
            best = (cut, answered - leaked, answered, leaked)
    return best[0], best[2], best[3]


def name(result: dict) -> str:  # type: ignore[type-arg]
    label = str(result["model"]).split("/")[-1]
    return label + (f" + {result['reranker'].split('/')[-1]}" if result.get("reranker") else "")


def hits(result: dict, form: str, k: int) -> int:  # type: ignore[type-arg]
    return sum(rank is not None and rank <= k for rank in result["ranks"][form])


def table(results: list[dict]) -> str:  # type: ignore[type-arg]
    """Three tables: cost per model, finding the right memory per language, and telling
    questions from small talk per language."""
    n = len(DATA["questions"])
    cost = [
        "| Model | Dim | Query ms p50 / p95 | Load s | RSS MB |",
        "|---|---|---|---|---|",
    ]
    found = [
        "| Model | " + " | ".join(FORMS) + " |",
        "|---|" + "---|" * len(FORMS),
    ]
    noise = [
        "| Model | " + " | ".join(SPOKEN) + " | all spoken |",
        "|---|" + "---|" * (len(SPOKEN) + 1),
    ]
    for r in results:
        cost.append(
            f"| {name(r)} | {r['dim']} | {r['query_ms_p50']} / {r['query_ms_p95']} | "
            f"{r['load_seconds']} | {r['peak_rss_mb']:.0f} |"
        )
        found.append(
            f"| {name(r)} | "
            + " | ".join(f"{hits(r, f, 1)} / {hits(r, f, TOP_K)}" for f in FORMS)
            + " |"
        )
        cells = []
        for forms in [[f] for f in SPOKEN] + [SPOKEN]:
            positives = [s for f in forms for s in r["expected_scores"][f]]
            negatives = [s for f in forms for s in r["negative_top"][f]]
            _, answered, leaked = best_threshold(r, forms)
            cells.append(f"{auc(positives, negatives):.2f} ({answered:.0%} / {leaked:.0%})")
        noise.append(f"| {name(r)} | " + " | ".join(cells) + " |")
    return (
        "Cost\n\n"
        + "\n".join(cost)
        + f"\n\nRight memory first / in the top {TOP_K}, out of {n}\n\n"
        + "\n".join(found)
        + "\n\nQuestions vs small talk: AUC (answered / small talk let through, at the best "
        "cut-off)\n\n" + "\n".join(noise)
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--rerank", help="comma-separated FastEmbed rerankers to add")
    parser.add_argument("--one", help=argparse.SUPPRESS)
    args = parser.parse_args()
    RESULTS.mkdir(exist_ok=True)

    def path(model: str, reranker: str | None) -> Path:
        name = model + (f"+{reranker}" if reranker else "")
        return RESULTS / f"{name.replace('/', '__')}.json"

    if args.one:
        model, _, reranker = args.one.partition("+")
        result = asyncio.run(measure(model, reranker or None))
        path(model, reranker or None).write_text(json.dumps(asdict(result), indent=1))
        return
    rerankers: list[str | None] = [None, *(args.rerank.split(",") if args.rerank else [])]
    results = []
    for model in args.models.split(","):
        for reranker in rerankers:
            run = model + (f"+{reranker}" if reranker else "")
            print(f"measuring {run}...", file=sys.stderr, flush=True)
            done = subprocess.run(
                [sys.executable, "-m", "bench.retrieval.run", "--one", run], cwd=ROOT, check=False
            )
            out = path(model, reranker)
            if done.returncode == 0 and out.exists():
                results.append(json.loads(out.read_text()))
    print(table(results))


if __name__ == "__main__":
    main()
