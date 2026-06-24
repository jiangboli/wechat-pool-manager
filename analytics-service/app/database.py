import os
import psycopg2
import psycopg2.pool
from fastapi import HTTPException

_pool = None

def init_pool():
    global _pool
    dsn = os.environ.get("CLAW_DO_DSN", "")
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    if not dsn:
        raise RuntimeError("CLAW_DO_DSN 环境变量未设置，无法连接 PG")
    _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn)
    return _pool

def get_conn():
    global _pool
    if _pool is None:
        init_pool()
    try:
        return _pool.getconn()
    except Exception as e:
        raise HTTPException(503, f"DB connection failed: {e}")

def put_conn(conn):
    global _pool
    if _pool and conn:
        _pool.putconn(conn)

def close_pool():
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None

def ensure_schema(conn):
    """幂等建表，每次启动时调用"""
    c = conn.cursor()
    c.execute("CREATE SCHEMA IF NOT EXISTS analytics")
    c.execute("""
        CREATE TABLE IF NOT EXISTS analytics.balance_deposits (
            id SERIAL PRIMARY KEY,
            label VARCHAR(100) NOT NULL,
            amount DECIMAL(12,2) NOT NULL,
            deposited_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            note VARCHAR(200)
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_balance_deposits_label_time
        ON analytics.balance_deposits(label, deposited_at DESC)
    """)
    conn.commit()
    c.close()
