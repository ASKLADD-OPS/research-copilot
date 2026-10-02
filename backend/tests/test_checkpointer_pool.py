"""Checkpointer 连接池契约测试。

`build_checkpointer()` 里有两处"不报错、但用不了"的陷阱，都是实测踩到的：

1. 用 `AsyncConnectionPool(open=False)` 去"试探" Postgres —— 池不连接，包一层
   saver 也不连接，于是 try 块永真，真正的连接失败被推迟到 `astream()` 内部才爆。
2. **池建出来没人 `open()`** —— 只要 Postgres 可达就会走到这条分支，第一次请求
   抛 `PoolClosed: the pool 'pool-2' is not open yet`，整轮流只剩一帧 error。

第 1 条靠"真连一次"的探测兜住了；第 2 条没有任何运行期信号会指向它
（`type(cp).__name__` 照样打印 `AsyncPostgresSaver`，日志也照样说图编译成功），
只能把构造参数钉死，所以单独一个文件。
"""

from __future__ import annotations

import pytest

from app.agents import checkpointer as ckpt


async def test_pool_handed_to_saver_must_be_open(monkeypatch):
    """契约：交给 `AsyncPostgresSaver` 的连接池必须是 open 的。

    写 `open=False` 时，构造、日志、`type(cp).__name__` 全部正常，
    坏掉只体现为"请求进来一个 token 都不出"，所以必须在这里挡。
    """
    import psycopg_pool

    # 必须先把 langgraph 真正导入完再替换类名：它的模块体里有
    # `AsyncConnectionPool[...]` 这种下标化的类型别名，换成一个普通类会让
    # 那句在导入期就 `TypeError: type ... is not subscriptable`，
    # 于是 build_checkpointer 走 except 分支退化成 MemorySaver，测试假绿。
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: F401

    monkeypatch.setattr(ckpt, "_postgres_usable_cached", lambda: True)
    captured: dict[str, object] = {}

    class _RecordingPool:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", _RecordingPool)

    ckpt.build_checkpointer()

    assert captured, "根本没构造连接池 —— 说明走进了降级分支，测试前提不成立"
    assert captured.get("open") is True, (
        "交给 saver 的池必须 open=True。open=False 时池从没被打开，"
        "第一次 astream 抛 PoolClosed，POST /chat/stream 只剩一帧 error"
    )
    assert captured.get("conninfo"), "池必须带 conninfo"


async def test_probe_failure_degrades_to_memory_saver(monkeypatch):
    """Postgres 连不上时必须退化，而不是留给请求一个用不了的 saver。"""
    from langgraph.checkpoint.memory import MemorySaver

    monkeypatch.setattr(ckpt, "_postgres_usable_cached", lambda: False)

    assert isinstance(ckpt.build_checkpointer(), MemorySaver)


@pytest.mark.unit
def test_probe_rejects_a_port_that_is_merely_listening(monkeypatch):
    """探测必须真连一次，不能只看端口通不通。

    本机就遇到过原生 PostgreSQL 在 5432 上监听、但角色密码不匹配：
    纯 TCP 探测会判"可用"，然后 graph 编译通过、请求时才炸。
    """
    import psycopg

    attempts: list[str] = []

    def _fail(conninfo: str, **kwargs: object) -> None:
        attempts.append(conninfo)
        raise psycopg.OperationalError("password authentication failed")

    monkeypatch.setattr(psycopg, "connect", _fail)

    assert ckpt._postgres_usable() is False
    assert attempts, "没真的发起过连接 —— 那就只是端口探测"
    assert attempts[0].startswith("postgresql://"), (
        "psycopg 不认 SQLAlchemy 的 postgresql+psycopg:// 方言前缀，必须剥掉"
    )


def _fake_psycopg(monkeypatch, *, has_table: bool, seen: list[str] | None = None) -> None:
    """把 `psycopg.connect` 换成"连得上"的假连接，只看 cursor 的返回值。"""

    class _Cursor:
        def execute(self, sql: str, *args: object, **kwargs: object) -> None:
            if seen is not None:
                seen.append(sql)

        def fetchone(self) -> tuple[bool]:
            return (has_table,)

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    class _Conn:
        def cursor(self) -> _Cursor:
            return _Cursor()

        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _Conn())


@pytest.mark.unit
def test_probe_rejects_postgres_that_lacks_checkpoint_tables(monkeypatch):
    """连得上但表没建好，同样必须判为不可用。

    只验连通性时，`setup()` 没跑成的 Postgres 会让每次 `astream` 抛
    `UndefinedTable: relation "checkpoints" does not exist`，
    `/chat/stream` 依旧一个 token 都出不来 —— 且没有任何运行期信号指向探测。
    表缺失是可恢复状态（启动时建表），不该是硬失败。
    """
    _fake_psycopg(monkeypatch, has_table=False)
    assert ckpt._postgres_usable() is False

    _fake_psycopg(monkeypatch, has_table=True)
    assert ckpt._postgres_usable() is True, "表在就该判可用，别把正常情况也降级了"


@pytest.mark.unit
def test_probe_actually_asks_about_the_checkpoint_table(monkeypatch):
    """防止有人把探测"简化"成返回 True —— 必须真去问表在不在。"""
    seen: list[str] = []
    _fake_psycopg(monkeypatch, has_table=True, seen=seen)

    assert ckpt._postgres_usable() is True
    assert any("checkpoints" in sql for sql in seen), f"探测没查 checkpoint 表，实际执行的 SQL：{seen}"


@pytest.mark.unit
def test_table_setup_does_not_go_through_build_checkpointer(monkeypatch):
    """建表必须独立于 `build_checkpointer()`，否则死锁：

    表不存在 ⇒ 探测为假 ⇒ 返回 MemorySaver ⇒ MemorySaver 没有 `setup()`
    ⇒ 表永远建不出来 ⇒ 永远降级。
    """
    import asyncio

    calls: list[str] = []

    def _boom() -> object:
        calls.append("build_checkpointer")
        raise AssertionError("建表不该复用 build_checkpointer：表缺失时它会返回 MemorySaver")

    monkeypatch.setattr(ckpt, "build_checkpointer", _boom)
    monkeypatch.setattr(ckpt, "_connect_ok", lambda: False)  # 连不上 → 直接返回

    asyncio.run(ckpt.init_checkpointer_tables())

    assert calls == [], "连不上时就去调 build_checkpointer 了 —— 死锁风险的来源"
