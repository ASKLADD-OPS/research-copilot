"""上下文装配测试 —— 引用白名单的"铸造车间"。

`to_context_block` 的编号顺序**就是** `[n]` 的合法取值范围：它变了，
生成侧写出的引用就全成了幻觉，溯源引擎会把正确答案判成失败。
所以这里钉的是编号规则与截断行为，不是文案格式。
"""

from __future__ import annotations

import pytest

from app.rag.fusion import dedupe_by_id, min_max_normalize, reciprocal_rank_fusion
from app.rag.retriever import RetrievedChunk, to_context_block

PAPER_ID = 123


def mk(index: int, content: str, **kw) -> RetrievedChunk:
    """chunk 主键是 int64 —— 与 Milvus 的 `chunk_id` 同一个值。"""
    return RetrievedChunk(id=index, paper_id=PAPER_ID, content=content, **kw)


@pytest.mark.unit
def test_numbers_start_at_one_and_are_contiguous():
    """编号必须是 1..n —— 生成侧写 [1] 就得指向第一块。"""
    block = to_context_block([mk(0, "第一块"), mk(1, "第二块"), mk(2, "第三块")])
    assert "[1]" in block
    assert "[2]" in block
    assert "[3]" in block
    assert "[4]" not in block
    assert block.index("[1]") < block.index("[2]") < block.index("[3]")


@pytest.mark.unit
def test_header_carries_locator_fields():
    """头部要能定位：paper / chunk / section / 页码，缺了就没法回溯原文。"""
    block = to_context_block([mk(7, "正文", section="Method", page=3)])
    head = block.split("\n")[0]
    assert "[1]" in head
    assert f"paper={PAPER_ID}" in head
    assert "chunk=7" in head
    assert "section=Method" in head
    assert "p.3" in head


@pytest.mark.unit
def test_page_is_rendered_once():
    """chunks.page 是单值。头部不能出现页码重复（早期 page_start/page_end 双值时
    同页会渲染成 "p.4-4"）。"""
    block = to_context_block([mk(0, "正文", page=4)])
    assert "p.4" in block
    assert "p.4-4" not in block


@pytest.mark.unit
def test_missing_optional_fields_are_omitted():
    block = to_context_block([mk(0, "正文")])
    head = block.split("\n")[0]
    assert "section=" not in head
    assert "p." not in head


@pytest.mark.unit
def test_blocks_are_separated_by_divider():
    block = to_context_block([mk(0, "甲"), mk(1, "乙")])
    assert "\n---\n" in block


@pytest.mark.unit
def test_content_is_stripped():
    block = to_context_block([mk(0, "  前后有空白  ")])
    assert "  前后有空白  " not in block
    assert "前后有空白" in block


@pytest.mark.unit
def test_max_chars_stops_adding_blocks():
    chunks = [mk(i, "内容" * 200) for i in range(10)]
    block = to_context_block(chunks, max_chars=600)
    assert block.count("[") < 10
    assert len(block) <= 600 + 200  # 允许最后一个块的头部开销


@pytest.mark.unit
def test_first_block_always_included_even_if_oversized():
    """单块超限也要给出去：否则上下文为空，生成侧只能凭空编。

    （这条不是理论风险 —— 早期实现就是 `if used + len(block) > max_chars: break`，
    一旦第一块就超限，整段上下文变空字符串。）
    """
    block = to_context_block([mk(0, "长" * 3000)], max_chars=100)
    assert "[1]" in block
    assert "长" * 3000 in block

    # 第二块才该被截掉
    block2 = to_context_block([mk(0, "长" * 300), mk(1, "第二块")], max_chars=320)
    assert "[1]" in block2
    assert "[2]" not in block2


@pytest.mark.unit
def test_empty_chunks_gives_empty_block():
    assert to_context_block([]) == ""


@pytest.mark.unit
def test_citation_whitelist_is_exactly_the_numbering():
    """把"编号即白名单"这条不变式写成断言：
    上下文里出现的最小编号是 1、最大编号等于块数。"""
    chunks = [mk(i, f"第 {i} 块内容") for i in range(5)]
    import re

    block = to_context_block(chunks)
    markers = [int(m) for m in re.findall(r"\[(\d+)\]", block)]
    assert min(markers) == 1
    assert max(markers) == len(chunks)
    assert sorted(set(markers)) == list(range(1, len(chunks) + 1))


# ---------------------------------------------------------------- 检索链路的合流
@pytest.mark.unit
def test_fusion_output_feeds_context_in_rank_order():
    """端到端小验证：RRF 的名次 → 上下文编号，顺序必须一致。

    dense 路：[1, 2]；sparse 路：[2, 3] → 2 被两路召回，融合后第一。
    """
    dense = [mk(1, "dense-1"), mk(2, "dense-2")]
    sparse = [mk(2, "sparse-1"), mk(3, "sparse-2")]
    fused = [c for c, _ in reciprocal_rank_fusion([dense, sparse])]
    assert [c.id for c in fused] == [2, 1, 3]

    block = to_context_block(fused)
    # [1] 必须就是融合后的第一名（id=2），且正文顺序与融合名次一致
    assert block.startswith("[1] ")
    assert "dense-2" in block.split("---")[0]  # id=2 的内容
    assert block.index("dense-2") < block.index("dense-1")  # id=2 排在 id=1 之前
    assert "[3]" in block

    deduped = dedupe_by_id(fused)
    assert len(deduped) == len(fused)
    normalized = min_max_normalize([s for _, s in reciprocal_rank_fusion([dense, sparse])])
    assert normalized[0] == 1.0
