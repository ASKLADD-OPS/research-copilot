"""分块器测试。

分块错了，检索与溯源都会错：引用定位不准、NLI 判错、答案被切断。
这里钉住的是它的**契约**（编号连续、长度上限、章节/页码透传、重叠），
不是具体的切分位置 —— 后者调参就会变，钉死了只会天天改测试。
"""

from __future__ import annotations

import pytest

from app.parsers.chunker import (
    MAX_CHARS,
    MIN_CHARS,
    OVERLAP_CHARS,
    TARGET_CHARS,
    Chunk,
    chunk_text,
    estimate_tokens,
)


# ---------------------------------------------------------------- token 估算
@pytest.mark.unit
def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0


@pytest.mark.unit
def test_estimate_tokens_chinese_counts_per_char():
    # 5 个汉字 = 5，再加非汉字部分的保底 1（`max(1, ...)`）→ 6
    assert estimate_tokens("中文四个字") == 6


@pytest.mark.unit
def test_estimate_tokens_english_is_chars_over_four():
    assert estimate_tokens("a" * 40) == 10


@pytest.mark.unit
def test_estimate_tokens_mixed():
    # 2 个汉字 + 8 个非汉字 → 2 + 2 = 4
    assert estimate_tokens("中文abcdefgh") == 4


# ---------------------------------------------------------------- Chunk
@pytest.mark.unit
def test_chunk_autofills_token_count():
    c = Chunk(index=0, content="检索增强生成")
    assert c.token_count == estimate_tokens("检索增强生成")
    assert c.token_count > 0


@pytest.mark.unit
def test_chunk_keeps_explicit_token_count():
    c = Chunk(index=0, content="内容", token_count=999)
    assert c.token_count == 999


# ---------------------------------------------------------------- 基本切分
@pytest.mark.unit
def test_empty_text_yields_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


@pytest.mark.unit
def test_short_text_is_one_chunk():
    chunks = chunk_text("这是一段很短的正文。")
    assert len(chunks) == 1
    assert chunks[0].content == "这是一段很短的正文。"
    assert chunks[0].index == 0


@pytest.mark.unit
def test_indices_are_continuous_from_start_index():
    text = "\n\n".join(f"第 {i} 段。" + "内容" * 40 for i in range(6))
    chunks = chunk_text(text, start_index=7)
    assert [c.index for c in chunks] == list(range(7, 7 + len(chunks)))


@pytest.mark.unit
def test_no_chunk_exceeds_max_chars():
    """单块不得越界 —— 越界会影响 embedding 截断与引用定位。"""
    text = "\n\n".join("句子。" * 200 for _ in range(8))
    for c in chunk_text(text):
        assert len(c.content) <= MAX_CHARS + OVERLAP_CHARS + 16, len(c.content)


@pytest.mark.unit
def test_long_paragraph_is_split_not_dropped():
    """单个超长段落必须被切开，而不是整段塞进一个 chunk 或直接丢弃。"""
    para = "这是一个很长的句子。" * 400
    chunks = chunk_text(para)
    assert len(chunks) > 1
    joined = "".join(c.content for c in chunks)
    # 内容只增不减（重叠会让它更长），不能丢字
    assert len(joined) >= len(para.replace("\n", ""))


@pytest.mark.unit
def test_section_and_page_propagate_to_every_chunk():
    text = "\n\n".join("段落内容。" * 60 for _ in range(4))
    chunks = chunk_text(text, section="Method", page_start=3, page_end=9)
    assert len(chunks) > 1
    assert all(c.section == "Method" for c in chunks)
    assert all(c.page_start == 3 and c.page_end == 9 for c in chunks)


@pytest.mark.unit
def test_overlap_carries_tail_into_next_chunk():
    """尾部重叠：把上一块的末段带到下一块开头，避免答案正好落在切缝上被切断。"""
    para1 = "甲" * 600  # 两块相加 1200 > TARGET_CHARS(900)，必然切开
    para2 = "乙" * 600
    chunks = chunk_text(f"{para1}\n\n{para2}")
    assert len(chunks) == 2
    assert chunks[0].content == para1
    assert chunks[1].content.startswith("甲" * OVERLAP_CHARS)
    assert chunks[1].content.endswith(para2)


@pytest.mark.unit
def test_tiny_trailing_fragment_merges_back():
    """末尾过短（< MIN_CHARS）的碎片并回上一块，不留噪声块。"""
    big = "主要段落内容。" * 60
    text = big + "\n\n短尾。"
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert "短尾。" in chunks[0].content


@pytest.mark.unit
def test_chunk_lengths_are_in_a_sane_band():
    text = "\n\n".join("正文句子。" * 80 for _ in range(5))
    chunks = chunk_text(text)
    assert len(chunks) >= 2
    # 除最后一块外，都应当接近目标长度而不是碎成小块
    for c in chunks[:-1]:
        assert len(c.content) >= MIN_CHARS
        assert len(c.content) <= TARGET_CHARS + MAX_CHARS


@pytest.mark.unit
def test_params_are_ordered():
    assert MIN_CHARS < TARGET_CHARS < MAX_CHARS
    assert 0 < OVERLAP_CHARS < TARGET_CHARS
