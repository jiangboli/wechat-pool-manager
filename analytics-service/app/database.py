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
        dsn = "host=125.67.215.86 dbname=claw_do user=claw_do_user password=dosh_13579"
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
