"""
Pool Proxy — Analytics 模块

在 proxy 异步队列中收集每次 LLM 调用的元数据，批量写入 PG。
不阻塞主请求流（fire-and-forget 模式）。
"""

import asyncio
import logging
import os
import time
from typing import Optional

import psycopg2
import psycopg2.pool

logger = logging.getLogger("pool_manager.analytics")

# ── PG 连接池 ────────────────────────────────────────────────────────

_pg_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_analytics_queue: Optional[asyncio.Queue] = None
_analytics_task: Optional[asyncio.Task] = None

# 环境变量默认值
DEFAULT_PG_HOST = os.environ.get("ANALYTICS_PG_HOST", "125.67.215.86")
DEFAULT_PG_PORT = int(os.environ.get("ANALYTICS_PG_PORT", "5432"))
DEFAULT_PG_USER = os.environ.get("ANALYTICS_PG_USER", "claw_do_user")
DEFAULT_PG_PASSWORD = os.environ.get("ANALYTICS_PG_PASSWORD", "dosh_13579")
DEFAULT_PG_DB = os.environ.get("ANALYTICS_PG_DB", "claw_do")

# 批量写入配置
BATCH_SIZE = 20          # 每 20 条或 5 秒写入一次
BATCH_INTERVAL_SEC = 5   # 最大间隔
QUEUE_MAX_SIZE = 2000    # 队列最大缓冲，超限丢弃


def init_analytics(pg_config: dict = None):
    """初始化 analytics 模块。

    Args:
        pg_config: PG 连接配置 dict（可选，默认从环境变量读取）
    """
    global _pg_pool, _analytics_queue, _analytics_task

    host = pg_config.get("host", DEFAULT_PG_HOST) if pg_config else DEFAULT_PG_HOST
    port = pg_config.get("port", DEFAULT_PG_PORT) if pg_config else DEFAULT_PG_PORT
    user = pg_config.get("user", DEFAULT_PG_USER) if pg_config else DEFAULT_PG_USER
    password = pg_config.get("password", DEFAULT_PG_PASSWORD) if pg_config else DEFAULT_PG_PASSWORD
    dbname = pg_config.get("dbname", DEFAULT_PG_DB) if pg_config else DEFAULT_PG_DB

    try:
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=3,
            host=host, port=port, user=user, password=password, dbname=dbname,
        )
        # 验证连接
        conn = _pg_pool.getconn()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        _pg_pool.putconn(conn)
        logger.info("Analytics PG 连接池就绪: %s:%s/%s", host, port, dbname)
    except Exception as e:
        logger.warning("Analytics PG 初始化失败（不影响 proxy 主流程）: %s", e)
        _pg_pool = None
        return

    _analytics_queue = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
    _create_task()


def _create_task():
    """启动后台写入协程。"""
    global _analytics_task
    if _analytics_task is None or _analytics_task.done():
        _analytics_task = asyncio.create_task(_batch_writer_loop())
        logger.info("Analytics 后台写入协程已启动")


async def _batch_writer_loop():
    """后台批量写入 PG。

    每 BATCH_SIZE 条数据或每 BATCH_INTERVAL_SEC 秒写入一次。
    """
    batch = []

    while True:
        try:
            # 等待第一批数据
            try:
                record = await asyncio.wait_for(
                    _analytics_queue.get(), timeout=BATCH_INTERVAL_SEC
                )
                batch.append(record)
            except asyncio.TimeoutError:
                continue  # 空队列，继续等

            # 积累到 BATCH_SIZE 条或超时
            while len(batch) < BATCH_SIZE:
                try:
                    record = await asyncio.wait_for(
                        _analytics_queue.get(), timeout=BATCH_INTERVAL_SEC
                    )
                    batch.append(record)
                except asyncio.TimeoutError:
                    break  # 超时，写入当前批次

            # 批量写入
            if batch:
                await _batch_insert(batch)
                batch.clear()

        except asyncio.CancelledError:
            # 任务取消前 flush 剩余数据
            if batch:
                await _batch_insert(batch)
            raise
        except Exception as e:
            logger.warning("Analytics 写入异常: %s", e)
            batch.clear()


async def _batch_insert(records: list):
    """批量插入记录到 PG。"""
    if not _pg_pool:
        return

    conn = None
    try:
        conn = _pg_pool.getconn()
        cur = conn.cursor()

        values = []
        params = []
        for r in records:
            values.append(
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            )
            params.extend([
                r.get("user_id", ""),
                r.get("model", ""),
                r.get("prompt_tokens", 0),
                r.get("completion_tokens", 0),
                r.get("total_tokens", 0),
                r.get("latency_ms", 0),
                r.get("msg_count", 0),
                r.get("status", "success"),
                r.get("error_type", ""),
                r.get("streaming", False),
                r.get("topic", "其他"),
                r.get("created_at", time.strftime("%Y-%m-%dT%H:%M:%S")),
            ])

        sql = f"""INSERT INTO analytics.proxy_api_calls
            (user_id, model, prompt_tokens, completion_tokens, total_tokens,
             latency_ms, msg_count, status, error_type, streaming, topic, created_at)
            VALUES {','.join(values)}"""

        cur.execute(sql, params)
        conn.commit()
        cur.close()
        logger.debug("Analytics 批量写入: %d 条记录", len(records))
    except Exception as e:
        logger.warning("Analytics 批量写入失败: %s", e)
        if conn:
            conn.rollback()
    finally:
        if conn and _pg_pool:
            _pg_pool.putconn(conn)


def _make_record(
    user_id: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    latency_ms: int = 0,
    msg_count: int = 0,
    status: str = "success",
    error_type: str = "",
    streaming: bool = False,
    topic: str = "其他",
) -> dict:
    """构造一条 analytics 记录。"""
    return {
        "user_id": user_id[:64],
        "model": model[:64],
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "latency_ms": latency_ms,
        "msg_count": msg_count,
        "status": status,
        "error_type": error_type[:32],
        "streaming": streaming,
        "topic": topic[:32],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def enqueue_record(record: dict):
    """将一条记录加入写入队列。

    非阻塞——队列满时丢弃。
    """
    global _analytics_queue
    if _analytics_queue is None:
        return
    try:
        _analytics_queue.put_nowait(record)
    except asyncio.QueueFull:
        logger.warning("Analytics 队列已满（%d），丢弃一条记录", QUEUE_MAX_SIZE)


def extract_tokens_from_response(response_data: dict) -> dict:
    """从 LLM 响应中提取 token 使用量。"""
    usage = response_data.get("usage", {})
    if isinstance(usage, dict):
        return {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def extract_msg_count(body: dict) -> int:
    """从请求 body 中提取消息轮数。"""
    messages = body.get("messages", [])
    return len(messages) if isinstance(messages, list) else 0


def infer_topic(body: dict) -> str:
    """从第一条用户消息推断话题分类。

    轻量关键词匹配，不做完整语义分析。
    """
    messages = body.get("messages", [])
    if not messages or not isinstance(messages, list):
        return "其他"

    # 找第一条 role=user 的消息
    first_user_msg = ""
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content", "")
            first_user_msg = content if isinstance(content, str) else str(content)
            break

    text = first_user_msg[:200].lower()

    # 关键词匹配（按优先级）
    keywords = [
        ("交易", ["买入", "卖出", "股票", "仓位", "持仓", "下单", "买卖"]),
        ("行情", ["行情", "涨跌", "价格", "k线", "涨幅", "跌幅", "走势", "报价"]),
        ("技术", ["代码", "编译", "bug", "部署", "函数", "报错", "git", "docker"]),
        ("资讯", ["新闻", "资讯", "公告", "政策", "消息", "报道"]),
    ]

    for topic_name, words in keywords:
        for word in words:
            if word in text:
                return topic_name

    # 检测聊天/问候
    greetings = ["你好", "hi", "hello", "早上好", "晚上好", "下午好", "在吗", "help", "帮助"]
    for g in greetings:
        if g in text:
            return "闲聊"

    return "其他"
