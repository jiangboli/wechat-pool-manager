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
DEFAULT_PG_HOST = os.environ.get("ANALYTICS_PG_HOST", "")
DEFAULT_PG_PORT = int(os.environ.get("ANALYTICS_PG_PORT", "5432"))
DEFAULT_PG_USER = os.environ.get("ANALYTICS_PG_USER", "")
DEFAULT_PG_PASSWORD = os.environ.get("ANALYTICS_PG_PASSWORD", "")
DEFAULT_PG_DB = os.environ.get("ANALYTICS_PG_DB", "")

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

    # 优先 ANALYTICS_PG_* 环境变量，后备解析 CLAW_DO_DSN
    host = DEFAULT_PG_HOST
    port = DEFAULT_PG_PORT
    user = DEFAULT_PG_USER
    password = DEFAULT_PG_PASSWORD
    dbname = DEFAULT_PG_DB
    if pg_config:
        host = pg_config.get("host", host)
        port = pg_config.get("port", port)
        user = pg_config.get("user", user)
        password = pg_config.get("password", password)
        dbname = pg_config.get("dbname", dbname)
    # 后备：从 CLAW_DO_DSN 解析
    if not host:
        dsn = os.environ.get("CLAW_DO_DSN", "")
        if dsn:
            try:
                # postgresql+asyncpg://user:password@host:port/dbname
                parts = dsn.split("://", 1)[-1]
                user_pass, rest = parts.split("@", 1)
                user, passwd = user_pass.split(":", 1)
                host_port, db = rest.split("/", 1)
                if ":" in host_port:
                    h, p = host_port.split(":", 1)
                    host = h
                    port = int(p)
                else:
                    host = host_port
                if not user: user = user
                if not password: password = passwd
                if not dbname: dbname = db
                logger.info("Analytics: 从 CLAW_DO_DSN 解析到 PG 连接 %s:%s/%s", host, port, dbname)
            except Exception as e:
                logger.debug("解析 CLAW_DO_DSN 失败: %s", e)

    try:
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=3,
            host=host, port=port, user=user, password=password, dbname=dbname,
            keepalives=1, keepalives_idle=60, keepalives_interval=10, keepalives_count=3,
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

        # 验证连接是否存活（防止 PG 侧超时断开的 stale 连接）
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
        except Exception:
            # stale 连接，丢弃并重拿
            try:
                _pg_pool.putconn(conn, close=True)
            except Exception:
                pass
            conn = _pg_pool.getconn()

        cur = conn.cursor()

        values = []
        params = []
        for r in records:
            values.append(
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
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
                r.get("username", ""),
                r.get("phone", ""),
                r.get("bot_name", ""),
            ])

        sql = f"""INSERT INTO analytics.proxy_api_calls
            (user_id, model, prompt_tokens, completion_tokens, total_tokens,
             latency_ms, msg_count, status, error_type, streaming, topic, created_at,
             username, phone, bot_name)
            VALUES {','.join(values)}"""

        cur.execute(sql, params)
        conn.commit()
        cur.close()
        logger.debug("Analytics 批量写入: %d 条记录", len(records))
    except Exception as e:
        logger.warning("Analytics 批量写入失败: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn and _pg_pool:
            try:
                _pg_pool.putconn(conn)
            except Exception:
                pass


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


# ── 话题分类（LLM 方式，不用关键词匹配） ────────────────────────────────────

TOPIC_NAMES = [
    "财经金融", "科技互联网", "社会时事", "娱乐八卦", "体育赛事",
    "游戏动漫", "音乐艺术", "教育学习", "健康医疗", "美食旅行",
    "生活消费", "汽车出行", "时尚美妆", "闲聊问候", "功能咨询",
]

TOPIC_EMOJI = {
    "财经金融": "📊", "科技互联网": "💻", "社会时事": "📰",
    "娱乐八卦": "🎬", "体育赛事": "⚽", "游戏动漫": "🎮",
    "音乐艺术": "🎵", "教育学习": "📚", "健康医疗": "💊",
    "美食旅行": "🍜", "生活消费": "🛒", "汽车出行": "🚗",
    "时尚美妆": "💄", "闲聊问候": "💬", "功能咨询": "🔧",
    "其他": "📌",
}

# 话题分类专用 API key（不消耗用户 key 的额度）
_CLASSIFY_API_KEY = os.environ.get("CLASSIFY_API_KEY", "")
_CLASSIFY_BASE_URL = os.environ.get("CLASSIFY_BASE_URL", "https://api.deepseek.com/v1")


async def classify_topic_llm(
    user_messages: list, bot_response: str,
    api_key: str = None, base_url: str = None,
) -> str:
    """使用 LLM 对对话内容进行话题分类（轻量级 deepseek-chat 调用，约百 token）。"""
    # 构建对话摘要
    conv_lines = []
    for msg in (user_messages or []):
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, str) and content:
            conv_lines.append(f"{role}: {content[:200].strip()}")
    conv_text = "\n".join(conv_lines[-6:])  # 最近 6 条消息

    # 如果 bot 有回复也附上
    if bot_response and len(bot_response) < 800:
        conv_text += f"\nassistant: {bot_response[:500].strip()}"

    if not conv_text.strip():
        return "其他"

    classify_body = {
        "model": "deepseek-chat",
        "messages": [
            {
                "role": "system",
                "content": (
                    f"你是一个对话话题分类器。根据以下对话内容，判断用户的问题属于哪个分类。\n\n"
                    f"可选分类：{'、'.join(TOPIC_NAMES)}\n\n"
                    f"只返回分类名称，不要任何其他内容。"
                ),
            },
            {"role": "user", "content": conv_text[:2000]},
        ],
        "max_tokens": 8,
        "temperature": 0,
    }

    try:
        # 优先使用传入的凭据，后备从环境变量读取
        classify_key = api_key or os.environ.get("CLASSIFY_API_KEY", "")
        classify_url = (base_url or os.environ.get("CLASSIFY_BASE_URL", "https://api.deepseek.com/v1")).rstrip("/")

        if not classify_key:
            return "其他"
        import httpx
        async with httpx.AsyncClient(timeout=10) as hc:
            resp = await hc.post(
                f"{classify_url}/chat/completions",
                json=classify_body,
                headers={"Authorization": f"Bearer {classify_key}", "Content-Type": "application/json"},
            )
            data = resp.json()
            topic = data["choices"][0]["message"]["content"].strip()
            if topic in TOPIC_NAMES:
                return topic
    except Exception:
        pass

    return "其他"
