"""Checkpointer —— 会话状态持久化，支撑多轮对话与断点续跑。

优先 Postgres（生产 / docker-compose），不可用时退化为内存版，
保证本机裸跑与单测也能起图。

**这里曾经有一段长期失效的降级逻辑**（2026-09-30 修复）：

原实现用 `AsyncConnectionPool(conninfo, open=False)` + `AsyncPostgresSaver(pool)`
来"试探"Postgres，失败就退化为 `MemorySaver`。但这两个调用**都不建立连接** ——
`open=False` 的池不连接，包一层 saver 也不连接。于是：

    - try 块在应用运行时**永远不抛异常**，降级分支根本走不到；
    - 唯一能触发降级的情形是"没有运行中的事件循环"（池构造会报
      `no running event loop`）—— 也就是**只有单测里会退化，线上不会**；
    - 真正的连接失败被推迟到 `graph.astream()` 内部才爆。表现为
      `POST /chat/stream` 连一个 token 都吐不出来，只回一帧 error。

现在改成**真正连一次**（带 `connect_timeout`）再决定，并把结论缓存 ——
checkpointer 的选择本来就是每进程一次的事（图是单例），缓存与既有生命周期一致。

同一天又补了第二层（更隐蔽）：**"连得上"不等于"能用"**。池打开后如果
`setup()` 没跑成、checkpoint 表不存在，每次请求仍会在 `astream` 里抛
`UndefinedTable`。两次都是同一类错误 —— *降级判断只验证了它假设成立的一半*。
所以探测现在同时验连通性与表存在，建表则走独立的连通性判断（否则会死锁：
表没建 ⇒ 探测假 ⇒ 退化 ⇒ 没人建表）。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.core.config import settings

# 每进程探测一次。图是单例，checkpointer 的选择本来就只做一次。
_probe_result: bool | None = None


def _conninfo() -> str:
    """psycopg 不认 SQLAlchemy 的 `postgresql+psycopg://` 方言前缀，要剥掉。"""
    return settings.sync_database_url.replace("postgresql+psycopg", "postgresql")


def _connect_ok() -> bool:
    """只判断"连得上"（网络 + 凭据），不判表。

    建表那步要用这个：若用 `_postgres_usable()` 就会形成死锁 ——
    表没建 ⇒ 探测为假 ⇒ 退化为 MemorySaver ⇒ 永远没人去建表。
    """
    try:
        import psycopg

        with psycopg.connect(_conninfo(), connect_timeout=min(settings.DB_CONNECT_TIMEOUT, 3)):
            return True
    except Exception as exc:  # noqa: BLE001 - 任何故障都只意味着"不可用"
        logger.info("Postgres 连接失败：{}", exc)
        return False


def _postgres_usable() -> bool:
    """连得上 **并且** checkpoint 表已就绪，才算可用。

    三层判断缺一不可：

    1. **不能用 `AsyncConnectionPool(open=False)` 判断** —— 见模块头注释。
    2. **不能用纯 socket 探测**：端口通不代表凭据对。本机就遇到过原生
       PostgreSQL 在 5432 上监听、但角色密码不匹配，纯 TCP 探测会误判为
       "可用"，然后照样在 astream 里炸掉。
    3. **连上了也要验表**（2026-10-02 补）。只验连通性时，一个"连得上但
       `setup()` 没跑成"的 Postgres 会让每次请求都抛
           UndefinedTable: relation "checkpoints" does not exist
       整个 `/chat/stream` 又是一帧 error 都吐不出内容 —— 而且这次连
       `PoolClosed` 那种运行期信号都没有，日志里只有一句建表失败的警告。
       表缺失是可恢复状态（启动时会建），不应该是硬失败。
    """
    conninfo = _conninfo()
    try:
        import psycopg

        with (
            psycopg.connect(conninfo, connect_timeout=min(settings.DB_CONNECT_TIMEOUT, 3)) as conn,
            conn.cursor() as cur,
        ):
            cur.execute("SELECT to_regclass('public.checkpoints') IS NOT NULL")
            row = cur.fetchone()
            has_tables = bool(row[0]) if row else False
    except Exception as exc:  # noqa: BLE001 - 任何故障都只意味着"不可用"
        logger.info("Postgres 探测失败：{}", exc)
        return False

    if not has_tables:
        logger.warning(
            "Postgres 可连但 checkpoint 表不存在（启动时 setup() 未成功）—— 退化为 MemorySaver，本轮会话状态不落库"
        )
    return has_tables


def _postgres_usable_cached() -> bool:
    global _probe_result
    if _probe_result is None:
        _probe_result = _postgres_usable()
    return _probe_result


def build_checkpointer() -> Any:
    """返回可用的 checkpointer：AsyncPostgresSaver > MemorySaver。"""
    from langgraph.checkpoint.memory import MemorySaver

    if not _postgres_usable_cached():
        logger.warning("Postgres 不可用，checkpointer 退化为 MemorySaver（会话状态不落库）")
        return MemorySaver()

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        # `open=True` 不能省。2026-10-02 实测：写 `open=False` 时池建出来没人 open，
        # 而两个构造调用都**不**建立连接，于是 try 块照常通过、`type(cp).__name__` 也
        # 照常打印 AsyncPostgresSaver，直到第一次 `astream()` 才抛
        #     PoolClosed: the pool 'pool-2' is not open yet (code 5000)
        # 整轮流只剩一帧 error。这条路径只在 Postgres **可达**时走到 ——
        # 也就是本机把密码修对之后立刻复发，而"连不上"时反而正常降级，
        # 所以极易被误判成"数据库还没好"。默认值本来就是 True，显式写出来防回退。
        pool = AsyncConnectionPool(
            conninfo=settings.sync_database_url.replace("postgresql+psycopg", "postgresql"),
            max_size=5,
            open=True,
        )
        saver = AsyncPostgresSaver(pool)
        logger.info("LangGraph checkpointer = AsyncPostgresSaver")
        return saver
    except Exception as exc:  # noqa: BLE001
        logger.warning("Postgres checkpointer 构造失败（{}），退化为 MemorySaver", exc)
        return MemorySaver()


async def init_checkpointer_tables() -> None:
    """启动时建表（幂等），并把探测结果预热进缓存。

    注意**不能**复用 `build_checkpointer()`：表不存在时它按设计返回
    `MemorySaver`，而 `MemorySaver` 没有 `setup()`，于是永远建不出表、
    永远降级。建表必须走独立的"只判断连通性"这条路。

    表建好后把缓存置空，让第一次请求重新判断 —— 否则这个进程会一直
    拿着建表之前的结论。
    """
    global _probe_result

    if not _connect_ok():
        logger.warning("Postgres 连不上，跳过 checkpointer 建表（会话状态不落库）")
        return

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(conninfo=_conninfo(), max_size=5, open=True)
        try:
            await AsyncPostgresSaver(pool).setup()
        finally:
            await pool.close()
        _probe_result = None
        logger.info("checkpointer 表已就绪")
    except Exception as exc:  # noqa: BLE001
        logger.warning("checkpointer 建表失败，会话状态不落库：{}", exc)


__all__ = ["build_checkpointer", "init_checkpointer_tables"]
