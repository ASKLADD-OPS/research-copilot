"""bge-m3 双向量 Embedding 测试 —— 覆盖验收标准 4（中英文测试通过并输出 dense + sparse）。

分两层，别混：

- **纯函数层**（`@pytest.mark.unit`）：`normalize_dense` / `top_weights` / `detect_device`。
  零依赖、毫秒级、永远跑。
- **真模型层**（`@pytest.mark.model`）：真的加载 `BAAI/bge-m3`，编码中英文各若干句。

模型层为什么断言**跨语言语义排序**，而不是"向量长度是 1024"：

    一个根本没加载成功的实现照样能吐出 1024 个数字。长度断言完全拦不住假阳性。
    `sim(猫, cat) > sim(猫, 飞机)` 才证明权重真的加载对了、且多语言语义空间是对齐的
    —— 这恰好也是最容易装错的地方（用 sentence-transformers 加载会拿到 dense 但
    丢掉 `sparse_linear` 头，稀疏向量静默退化成空或词法权重）。

跑模型层需要约 2GB 权重（ModelScope 拉取，只下一次）：

    pytest -m model -s tests/test_embeddings.py

`-s` 是必须的：本文件会用 `print` 把 dense / sparse **打出来**，验收标准要求"输出"。
"""

from __future__ import annotations

import math
import statistics

import pytest

from app.embeddings.bge_m3 import (
    DENSE_DIM,
    aencode_dense,
    aencode_query,
    aencode_sparse,
    ahealth,
    detect_device,
    get_embedder,
    normalize_dense,
    top_weights,
)

# 中英对照语料。用**真正的平行译文**，不要用"自己改写的英文"——实测 bge-m3 给
# 松散改写的相似度只有 0.71，会给阈值断言埋一个假警报（本项目就踩过）。
# 用句子而不是单词，是因为稀疏头对单字的权重分布太平，排序区分度不如句子稳定。
# 实测参考（2026-09-30，FlagEmbedding/bge-m3/CPU）：
#   同句自比 1.0000 | 本文提出… 0.8500 | 天气 0.9386 | 猫 0.7506 | 不相关 0.3132
ZH = "本文提出了一种新的混合检索方法。"
EN_SAME = "This paper proposes a novel hybrid retrieval method."
EN_OTHER = "The photovoltaic cell converts sunlight into electricity."


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。dense 已 L2 归一化，点积即余弦，但这里不依赖那个前提。"""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    assert na > 0 and nb > 0, "零向量说明模型没出正常输出"
    return dot / (na * nb)


# ==================================================================== 纯函数层
@pytest.mark.unit
def test_normalize_dense_gives_unit_vector():
    out = normalize_dense([3.0, 4.0])
    assert out == pytest.approx([0.6, 0.8])
    assert math.isclose(math.sqrt(sum(v * v for v in out)), 1.0, rel_tol=1e-12)


@pytest.mark.unit
def test_normalize_dense_zero_vector_is_returned_untouched():
    """零向量不能除零崩掉 —— 全零 chunk（如只含公式编号）会走到这条路径。"""
    assert normalize_dense([0.0, 0.0]) == [0.0, 0.0]


@pytest.mark.unit
def test_top_weights_keeps_only_the_heaviest_tokens():
    weights = {i: float(i) for i in range(500)}

    out = top_weights(weights, 10)

    assert len(out) == 10
    assert set(out) == set(range(490, 500))  # 权重最大的 10 个，不是前 10 个


@pytest.mark.unit
def test_top_weights_passes_through_when_already_small():
    weights = {7: 0.5, 9: 0.25}
    assert top_weights(weights, 256) == weights


@pytest.mark.unit
def test_detect_device_returns_a_usable_name():
    assert detect_device("cpu") == "cpu"
    assert detect_device() in {"cpu", "cuda", "mps"}


# ==================================================================== 真模型层
@pytest.fixture(scope="session")
def embedder():
    """会话级单例。bge-m3 加载一次要几十秒，绝不能每个用例加载一遍。"""
    model = get_embedder()
    try:
        model.encode(["warmup"])
    except Exception as exc:  # noqa: BLE001 - 依赖缺失 / 权重拉不到，都要给出可读原因
        pytest.skip(f"bge-m3 不可用，跳过模型层：{type(exc).__name__}: {exc}")

    if model.backend != "bge-m3":
        pytest.fail(
            f"bge-m3 走了降级路径（backend={model.backend}）。"
            "learned sparse 只能从 FlagEmbedding.BGEM3FlagModel 拿到；"
            "请 pip install FlagEmbedding 并确认 ModelScope 权重可下载。"
        )
    return model


@pytest.mark.model
def test_dense_is_1024_dim_and_l2_normalized(embedder):
    res = embedder.encode([ZH, EN_SAME])

    assert res.backend == "bge-m3"
    assert len(res.dense) == 2
    for vec in res.dense:
        assert len(vec) == DENSE_DIM == 1024
        assert math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0, rel_tol=1e-4)
        assert all(isinstance(v, float) for v in vec)


@pytest.mark.model
def test_cross_lingual_semantics_are_aligned(embedder):
    """中文句和它的英文译文必须比不相关英文句更近 —— 这是"权重装对了"的判据。"""
    zh, en_same, en_other = embedder.encode_dense([ZH, EN_SAME, EN_OTHER])

    sim_translation = cosine(zh, en_same)
    sim_unrelated = cosine(zh, en_other)

    assert sim_translation > sim_unrelated, f"跨语言对齐失败: {sim_translation:.4f} <= {sim_unrelated:.4f}"
    # 绝对阈值：实测真译文落在 0.75~0.94，不相关约 0.31，留 0.10 余量防模型换版本后失守。
    # 阈值取 0.75 而不是更高，是因为"猫"这类短句译文只到 0.7506 —— 那是实测下限。
    assert sim_translation > 0.75, f"跨语言相似度偏低（{sim_translation:.4f}），权重可能没装对"


@pytest.mark.model
def test_chinese_pair_closer_than_chinese_unrelated(embedder):
    """同语言内部也要有区分度 —— 否则可能整批向量退化成同一个方向。"""
    a, b, c = embedder.encode_dense(
        [
            "图神经网络用于节点分类。",
            "图卷积网络在引文网络中做节点分类。",
            "今天天气很好，适合去公园散步。",
        ]
    )
    assert cosine(a, b) > cosine(a, c)


@pytest.mark.model
def test_sparse_is_learned_lexical_weights(embedder):
    """稀疏向量必须是 {token_id: weight} 且权重为正。

    bge-m3 的稀疏是 SPLADE 家族的 learned sparse：key 是词表 id、value 是学出来的
    重要性。如果这里拿到的是空 dict，说明稀疏头没加载上（多半是用 ST 加载的）。
    """
    sparse = embedder.encode_sparse([ZH])[0]

    assert sparse, "稀疏向量为空 —— sparse_linear 头没加载上"
    assert all(isinstance(k, int) for k in sparse), "key 必须是 token id（int）"
    assert all(isinstance(v, float) and v > 0 for v in sparse.values())
    # Milvus 的 SPARSE_INVERTED_INDEX 以 key 为词，id 必须能塞进 uint32
    assert all(0 <= k < 2**32 for k in sparse)


@pytest.mark.model
def test_sparse_top_n_truncation_is_enforced(embedder):
    long_text = ZH * 30

    full = embedder.encode_sparse([long_text])[0]
    limited = embedder.encode([long_text], top_n=8).sparse[0]

    assert len(limited) <= 8
    if len(full) > 8:
        assert set(limited) <= set(full), "截断只能从原向量里取，不能引入新项"


@pytest.mark.model
def test_hybrid_returns_dense_and_sparse_in_one_pass(embedder):
    texts = [ZH, EN_SAME]
    full = embedder.encode(texts)

    hybrid = embedder.encode_hybrid(texts)

    assert len(hybrid) == 2
    for i, item in enumerate(hybrid):
        assert item.dense == full.dense[i]
        assert item.sparse == full.sparse[i]


@pytest.mark.model
def test_encoder_is_deterministic(embedder):
    """两次编码同一句必须一致 —— 否则检索结果无法复现，也没法做缓存。"""
    first = embedder.encode_dense([ZH])[0]
    second = embedder.encode_dense([ZH])[0]
    assert first == pytest.approx(second)


@pytest.mark.model
async def test_async_wrappers_offload_to_executor(embedder):
    texts = [ZH, EN_SAME]

    dense = await aencode_dense(texts)
    sparse = await aencode_sparse(texts)
    single = await aencode_query("混合检索")

    assert len(dense) == len(sparse) == 2
    assert len(dense[0]) == DENSE_DIM
    assert len(single.dense) == 1 and single.sparse


@pytest.mark.model
async def test_health_reports_backend_and_dims(embedder):
    ok, detail = await ahealth()

    assert ok is True
    assert "bge-m3" in detail
    assert f"dense_dim={DENSE_DIM}" in detail


@pytest.mark.model
def test_print_dense_and_sparse(embedder):
    """验收标准 4 明写"输出 dense + sparse" —— 这个用例就是那一步的可见证据。

    ⚠ 这里刻意**不用 `capsys` fixture**：pytest 的 `capsys` 会为该用例重新开启捕获，
    把 `-s`（`--capture=no`）顶掉 —— 断言能过，但屏幕上什么都看不到，而验收要求是
    "输出"。所以先拼成一份 report 字符串，`print` 出去，再对同一份字符串断言。
    """
    texts = ["混合检索 hybrid retrieval", "reciprocal rank fusion 倒数排名融合"]
    res = embedder.encode(texts)

    lines = [f"backend={res.backend}　dense_dim={len(res.dense[0])}"]
    for text, dense, sparse in zip(texts, res.dense, res.sparse, strict=True):
        l2 = math.sqrt(sum(v * v for v in dense))
        head = ", ".join(f"{v:+.6f}" for v in dense[:8])
        top = dict(sorted(sparse.items(), key=lambda kv: -kv[1])[:8])
        lines += [
            f"[文本] {text}",
            f"[dense] dim={len(dense)} L2={l2:.6f}",
            f"[dense][:8] [{head}, ...]",
            f"[sparse] 非零项={len(sparse)} 权重均值={statistics.fmean(sparse.values()):.4f}",
            f"[sparse][:8] {top}",
        ]
    report = "\n".join(lines)
    print("\n" + report)

    assert "[dense][:8]" in report and "[sparse][:8]" in report
