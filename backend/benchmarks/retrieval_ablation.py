"""检索链路消融实验 —— 用项目自身的 bge-m3 与 RRF 实现产出可复现的对比数据。

跑法（backend 根目录）：
    python benchmarks/retrieval_ablation.py --json ../docs/figures/retrieval_ablation.json

对比：dense-only / sparse-only / RRF(k=60)，另做 RRF k 值敏感性。
指标：Recall@5、MRR@10、nDCG@10。

标注集是**自建的小规模集合**（40 chunk / 12 query），用于验证融合机制本身，
不代表大规模语料上的绝对水平 —— 这句同样写在技术文档里。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag.fusion import rrf_fuse  # noqa: E402


@dataclass
class Doc:
    id: str
    text: str = ""
    sources: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 标注语料
CORPUS: list[tuple[str, str]] = [
    ("c01", "BGE-M3 jointly performs dense retrieval, sparse retrieval and multi-vector retrieval within one model."),
    ("c02", "HNSW is a graph-based approximate nearest neighbour index that offers logarithmic search complexity."),
    ("c03", "Reciprocal Rank Fusion combines ranked lists using score(d) = sum over runs of 1/(k + rank(d))."),
    ("c04", "The default RRF constant k = 60 was empirically chosen and is insensitive across TREC collections."),
    ("c05", "Cross-encoder rerankers jointly encode query and passage, yielding higher precision at higher latency."),
    ("c06", "Product quantization compresses vectors into short codes so that an index fits into main memory."),
    ("c07", "Retrieval augmented generation grounds generation in retrieved evidence to reduce hallucination."),
    ("c08", "Corrective RAG grades retrieved documents and triggers query rewriting or web search when needed."),
    ("c09", "Self-RAG trains a model to emit reflection tokens deciding when to retrieve and when to critique."),
    ("c10", "Chain-of-thought prompting elicits intermediate reasoning steps before producing the final answer."),
    ("c11", "Plan-and-solve prompting first decomposes a task into subtasks and then executes them sequentially."),
    ("c12", "ReAct interleaves reasoning traces with tool-calling actions in a single loop."),
    ("c13", "Reflexion stores verbal self-reflections in an episodic memory to improve subsequent attempts."),
    ("c14", "The Model Context Protocol standardises how applications expose tools and resources to language models."),
    ("c15", "A structured tool schema lets the model choose arguments reliably instead of parsing free-form text."),
    ("c16", "Sandboxing executes untrusted generated code under restricted imports, memory and wall-clock limits."),
    ("c17", "Natural language inference models predict entailment, contradiction or neutrality between two texts."),
    ("c18", "Grounding ratio is the fraction of content terms in an answer that are supported by retrieved evidence."),
    ("c19", "Citation attribution checks whether every numbered marker in the answer maps to a retrieved document."),
    ("c20", "Prompt injection attacks embed instructions in untrusted text to hijack the model's behaviour."),
    ("c21", "Dense embeddings map semantically similar passages to nearby points in a high dimensional space."),
    ("c22", "Sparse lexical weights assign importance to individual tokens, capturing rare term matches."),
    ("c23", "Multi-vector retrieval aggregates token level similarities, also called late interaction."),
    ("c24", "Hybrid search merges dense and sparse candidates, improving recall over either route alone."),
    ("c25", "Long context models can ingest thousands of tokens but still lose information in the middle."),
    ("c26", "Semantic chunking splits documents at topic boundaries rather than at a fixed number of characters."),
    ("c27", "Page-level bounding boxes let a reader highlight the exact source region of a quoted sentence."),
    ("c28", "Two-column academic PDFs must be re-ordered so that reading order follows the columns."),
    ("c29", "Optical character recognition recovers text from scanned pages that carry no text layer."),
    ("c30", "Citation graphs model papers as nodes and references as directed edges."),
    ("c31", "PageRank scores a node by the number and importance of the nodes that point to it."),
    ("c32", "Community detection partitions a graph into densely connected groups by modularity maximisation."),
    ("c33", "Betweenness centrality identifies nodes that act as bridges between separate clusters."),
    ("c34", "向量检索把查询和文档编码成稠密向量，再用余弦相似度比较它们的方向。"),
    ("c35", "稀疏检索依靠词项匹配，对专有名词和罕见术语特别有效，但无法处理同义改写。"),
    ("c36", "混合检索同时跑稠密与稀疏两路，再融合排名，通常比单路召回更稳。"),
    ("c37", "重排序模型对召回的候选做精细打分，代价是延迟更高，所以只对少量候选用。"),
    ("c38", "幻觉是指模型生成了检索材料里并不存在的事实性内容。"),
    ("c39", "意图识别把用户问题分成若干类别，低置信度时先向用户澄清。"),
    ("c40", "计划与执行分离：先规划出有依赖关系的步骤图，再按就绪集合并发执行。"),
]

# (查询, 相关 chunk, 类型)
QUERIES: list[tuple[str, list[str], str]] = [
    ("What does HNSW stand for and what does it do?", ["c02"], "lexical"),
    ("Which model unifies dense sparse and multi-vector retrieval?", ["c01"], "lexical"),
    ("What is the default k in Reciprocal Rank Fusion?", ["c03", "c04"], "lexical"),
    ("Describe the Reflexion self-reflection memory.", ["c13"], "lexical"),
    ("What does Corrective RAG do when retrieved documents look bad?", ["c08"], "lexical"),
    ("怎样让索引小到能整个塞进内存里？", ["c06"], "semantic"),
    ("怎么避免模型编造材料里没有的内容？", ["c07", "c18", "c38"], "semantic"),
    ("文档太长时切块应该切在哪里比较好？", ["c26"], "semantic"),
    ("怎么判断答案里每个论断是不是真有出处？", ["c17", "c18", "c19"], "semantic"),
    ("扫描出来没有文字层的论文要怎么处理？", ["c29"], "semantic"),
    ("工具协议 schema 对模型选参数有什么帮助？", ["c14", "c15"], "mixed"),
    ("引文网络里怎么找出连接不同领域的枢纽论文？", ["c30", "c33"], "mixed"),
]

KS = (10, 20, 40, 60, 100)


def recall_at_k(ranked: list[str], gold: set[str], k: int) -> float:
    return sum(1 for d in ranked[:k] if d in gold) / len(gold) if gold else 0.0


def mrr_at_k(ranked: list[str], gold: set[str], k: int) -> float:
    return next((1.0 / i for i, d in enumerate(ranked[:k], 1) if d in gold), 0.0)


def ndcg_at_k(ranked: list[str], gold: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 1) for i, d in enumerate(ranked[:k], 1) if d in gold)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(gold), k) + 1))
    return dcg / ideal if ideal else 0.0


def mean_metrics(per_query: list[list[str]]) -> dict[str, float]:
    rows = [
        (
            recall_at_k(r, set(g), 5),
            recall_at_k(r, set(g), 20),
            mrr_at_k(r, set(g), 10),
            ndcg_at_k(r, set(g), 10),
        )
        for r, (_, g, _) in zip(per_query, QUERIES)
    ]
    n = len(rows)
    return {
        "recall@5": round(sum(x[0] for x in rows) / n, 4),
        "recall@20": round(sum(x[1] for x in rows) / n, 4),
        "mrr@10": round(sum(x[2] for x in rows) / n, 4),
        "ndcg@10": round(sum(x[3] for x in rows) / n, 4),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    import numpy as np
    from FlagEmbedding import BGEM3FlagModel

    print("[1/4] 加载 bge-m3（走 ModelScope 本地快照，与 app/embeddings 同一路径）...", flush=True)
    from modelscope import snapshot_download

    from app.embeddings.bge_m3 import MODELSCOPE_IGNORE_PATTERNS

    local_dir = snapshot_download("BAAI/bge-m3", ignore_file_pattern=list(MODELSCOPE_IGNORE_PATTERNS))
    model = BGEM3FlagModel(local_dir, use_fp16=False)

    print("[2/4] 编码 40 chunk + 12 query ...", flush=True)
    docs = [Doc(cid, text) for cid, text in CORPUS]
    d_dense = np.asarray(model.encode([d.text for d in docs], batch_size=8, max_length=512,
                                      return_dense=True, return_sparse=False,
                                      return_colbert_vecs=False)["dense_vecs"])
    q_dense = np.asarray(model.encode([q for q, _, _ in QUERIES], batch_size=8, max_length=512,
                                      return_dense=True, return_sparse=False,
                                      return_colbert_vecs=False)["dense_vecs"])
    d_sparse = model.encode([d.text for d in docs], batch_size=8, max_length=512,
                            return_dense=False, return_sparse=True,
                            return_colbert_vecs=False)["lexical_weights"]
    q_sparse = model.encode([q for q, _, _ in QUERIES], batch_size=8, max_length=512,
                            return_dense=False, return_sparse=True,
                            return_colbert_vecs=False)["lexical_weights"]

    def lex_dot(a: dict, b: dict) -> float:
        if len(a) > len(b):
            a, b = b, a
        return sum(float(v) * float(b[t]) for t, v in a.items() if t in b)

    # 查询归一化：bge-m3 的 dense 未归一化，用余弦
    q_dense_n = q_dense / (np.linalg.norm(q_dense, axis=1, keepdims=True) + 1e-12)
    d_dense_n = d_dense / (np.linalg.norm(d_dense, axis=1, keepdims=True) + 1e-12)

    print("[3/4] 计算 dense / sparse / RRF 排名 ...", flush=True)
    per_query: dict[str, list[list[str]]] = {"dense-only": [], "sparse-only": []}
    per_query.update({f"RRF(k={k})": [] for k in KS})
    for qi in range(len(QUERIES)):
        dsim = d_dense_n @ q_dense_n[qi]
        ssim = [lex_dot(q_sparse[qi], dw) for dw in d_sparse]
        dr = [docs[i].id for i in np.argsort(-dsim)]
        sr = [docs[i].id for i in np.argsort(-np.asarray(ssim))]
        per_query["dense-only"].append(dr)
        per_query["sparse-only"].append(sr)
        for k in KS:
            fused = rrf_fuse([docs[i] for i in np.argsort(-dsim)],
                             [docs[i] for i in np.argsort(-np.asarray(ssim))], k=k)
            per_query[f"RRF(k={k})"].append([d.id for d, _ in fused])

    print("[4/4] 汇总指标 ...", flush=True)
    summary = {name: mean_metrics(v) for name, v in per_query.items()}

    # 候选召回阶段（RRF 真正发挥作用的位置）：dense / sparse / 两路并集 各有几条 gold 进 top-20
    cand = {"dense_candidates": 0, "sparse_candidates": 0, "union_candidates": 0, "gold_total": 0}
    union_beats_dense = 0
    for i, (_, gold, _) in enumerate(QUERIES):
        g = set(gold)
        d20, s20 = set(per_query["dense-only"][i][:20]), set(per_query["sparse-only"][i][:20])
        cand["dense_candidates"] += len(g & d20)
        cand["sparse_candidates"] += len(g & s20)
        cand["union_candidates"] += len(g & (d20 | s20))
        cand["gold_total"] += len(g)
        if len(g & (d20 | s20)) > len(g & d20):
            union_beats_dense += 1
    cand["dense_candidate_recall"] = round(cand["dense_candidates"] / cand["gold_total"], 4)
    cand["sparse_candidate_recall"] = round(cand["sparse_candidates"] / cand["gold_total"], 4)
    cand["union_candidate_recall"] = round(cand["union_candidates"] / cand["gold_total"], 4)
    cand["queries_where_union_beats_dense"] = f"{union_beats_dense}/{len(QUERIES)}"

    by_type = {}
    for qtype in ("lexical", "semantic", "mixed"):
        idx = [i for i, (_, _, t) in enumerate(QUERIES) if t == qtype]
        row = {}
        for name in ("dense-only", "sparse-only", "RRF(k=60)"):
            row[name] = round(sum(ndcg_at_k(per_query[name][i], set(QUERIES[i][1]), 10)
                                  for i in idx) / len(idx), 4)
        by_type[qtype] = row

    report = {
        "meta": {"n_chunks": len(CORPUS), "n_queries": len(QUERIES),
                 "model": "BAAI/bge-m3", "rrf_k_default": 60,
                 "note": "自建小规模标注集，用于验证融合机制，非大规模基准"},
        "summary": summary,
        "candidate_recall": cand,
        "by_type_ndcg@10": by_type,
        "k_sensitivity": {str(k): summary[f"RRF(k={k})"] for k in KS},
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print("\n[done] ->", p)


if __name__ == "__main__":
    main()
