"""CRAG 零成本预判的实测：两级判级到底省掉了多少 LLM 调用，以及它在什么情况下判错。

复用 `retrieval_ablation.py` 的语料与查询，取 dense top-5 当作"检索结果"，
直接调用 `app.rag.crag.cheap_grade`。

跑法：python benchmarks/crag_gate.py --json ../docs/figures/crag_gate.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag.crag import CHEAP_SKIP_HIGH, CHEAP_SKIP_LOW, cheap_grade, lexical_coverage  # noqa: E402
from benchmarks.retrieval_ablation import CORPUS, QUERIES  # noqa: E402


@dataclass
class Chunk:
    id: str
    content: str


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()

    import numpy as np
    from FlagEmbedding import BGEM3FlagModel
    from modelscope import snapshot_download

    from app.embeddings.bge_m3 import MODELSCOPE_IGNORE_PATTERNS

    print("[1/2] 加载 bge-m3 ...", flush=True)
    local = snapshot_download("BAAI/bge-m3", ignore_file_pattern=list(MODELSCOPE_IGNORE_PATTERNS))
    model = BGEM3FlagModel(local, use_fp16=False)

    docs = [Chunk(cid, text) for cid, text in CORPUS]
    d = np.asarray(model.encode([c.content for c in docs], batch_size=8, max_length=512,
                                return_dense=True, return_sparse=False,
                                return_colbert_vecs=False)["dense_vecs"])
    q = np.asarray(model.encode([x for x, _, _ in QUERIES], batch_size=8, max_length=512,
                                return_dense=True, return_sparse=False,
                                return_colbert_vecs=False)["dense_vecs"])
    d /= np.linalg.norm(d, axis=1, keepdims=True) + 1e-12
    q /= np.linalg.norm(q, axis=1, keepdims=True) + 1e-12

    print("[2/2] 逐条跑 cheap_grade ...", flush=True)
    rows = []
    for qi, (query, gold, qtype) in enumerate(QUERIES):
        order = np.argsort(-(d @ q[qi]))[: args.top]
        retrieved = [docs[int(i)] for i in order]
        cov = lexical_coverage(query, retrieved)
        g = cheap_grade(query, retrieved)
        rows.append({
            "query": query,
            "type": qtype,
            "coverage": round(cov, 4),
            "verdict": g.verdict if g else "NEEDS_LLM",
            "gold_retrieved": bool(set(gold) & {r.id for r in retrieved}),
        })

    dist = Counter(r["verdict"] for r in rows)
    # 判级正确性：gold 确实在 top-k 里（说明检索是成功的），但预判给出了 irrelevant
    false_irrelevant = [r for r in rows if r["gold_retrieved"] and r["verdict"] == "irrelevant"]
    by_lang = {}
    for lang, test in (("zh", lambda s: any("\u4e00" <= c <= "\u9fff" for c in s)),
                       ("en", lambda s: not any("\u4e00" <= c <= "\u9fff" for c in s))):
        sub = [r for r in rows if test(r["query"])]
        by_lang[lang] = {
            "n": len(sub),
            "mean_coverage": round(sum(r["coverage"] for r in sub) / len(sub), 4) if sub else None,
            "skipped_llm": sum(1 for r in sub if r["verdict"] != "NEEDS_LLM"),
        }

    report = {
        "meta": {"n_queries": len(rows), "top_k": args.top,
                 "cheap_skip_low": CHEAP_SKIP_LOW, "cheap_skip_high": CHEAP_SKIP_HIGH,
                 "note": "语料为英文为主 + 少量中文；查询中英各半，用于暴露跨语言下的预判行为"},
        "verdict_distribution": dict(dist),
        "llm_skipped": sum(1 for r in rows if r["verdict"] != "NEEDS_LLM"),
        "needs_llm": dist.get("NEEDS_LLM", 0),
        "false_irrelevant_when_gold_retrieved": len(false_irrelevant),
        "by_query_language": by_lang,
        "rows": rows,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print("[done] ->", p)


if __name__ == "__main__":
    main()
