from fastapi import APIRouter
from database import get_conn, put_conn

router = APIRouter(tags=["health"])

@router.get("/health")
async def health_check():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT 1")
        c.fetchone()
        return {"status": "ok", "service": "analytics-service", "version": "1.0.0"}
    finally:
        put_conn(conn)

@router.get("/health/containers")
async def container_health():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT container_name, status, restart_count, machine_ip, memory_limit FROM public.docker_containers ORDER BY machine_ip, container_name")
        return [{"name": r[0], "status": r[1], "restarts": r[2], "machine": r[3], "memory": r[4]} for r in c.fetchall()]
    finally:
        put_conn(conn)

@router.get("/health/gateways")
async def gateway_health():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT DISTINCT ON (profile_name) profile_name, status, restart_count, memory_usage, error_message, checked_at FROM public.gateway_health_log ORDER BY profile_name, checked_at DESC")
        return [{"profile": r[0], "status": r[1], "restarts": r[2], "memory": r[3], "error": r[4], "checked_at": str(r[5])} for r in c.fetchall()]
    finally:
        put_conn(conn)

@router.get("/health/machines")
async def machine_health():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT machine_ip, hostname, status, total_slots, hot_pool_size, last_heartbeat_at FROM public.machines ORDER BY machine_ip")
        return [{"ip": r[0], "hostname": r[1], "status": r[2], "slots": r[3], "hot_pool": r[4], "heartbeat": str(r[5])} for r in c.fetchall()]
    finally:
        put_conn(conn)
