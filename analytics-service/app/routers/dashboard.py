from fastapi import APIRouter
from database import get_conn, put_conn

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard")
async def get_dashboard():
    conn = get_conn()
    try:
        c = conn.cursor()
        data = {}

        c.execute("SELECT count(*), COALESCE(SUM(total_tokens),0), count(*) FILTER (WHERE error_type != ''), COALESCE(round(AVG(latency_ms)),0) FROM analytics.proxy_api_calls WHERE created_at >= CURRENT_DATE")
        r = c.fetchone()
        data["today_calls"] = r[0]
        data["today_tokens"] = r[1]
        data["today_errors"] = r[2]
        data["avg_latency"] = r[3]

        c.execute("SELECT count(DISTINCT user_id) FROM analytics.proxy_api_calls WHERE created_at >= CURRENT_DATE - 7")
        data["active_users_7d"] = c.fetchone()[0]

        c.execute("SELECT count(*) FROM public.bindings WHERE status != 'unbound'")
        data["total_users"] = c.fetchone()[0]

        c.execute("SELECT count(*) FROM public.bindings WHERE bound_at >= CURRENT_DATE AND status != 'unbound'")
        data["today_new_users"] = c.fetchone()[0]

        c.execute("SELECT COALESCE(NULLIF(username,''), user_id) AS display_name, count(*) FROM analytics.proxy_api_calls WHERE created_at >= CURRENT_DATE - 7 GROUP BY display_name ORDER BY count(*) DESC LIMIT 10")
        data["top_req"] = [[r[0], r[1]] for r in c.fetchall()]

        c.execute("SELECT COALESCE(NULLIF(username,''), user_id) AS display_name, COALESCE(SUM(total_tokens),0) FROM analytics.proxy_api_calls WHERE created_at >= CURRENT_DATE - 7 GROUP BY display_name ORDER BY SUM(total_tokens) DESC LIMIT 10")
        data["top_tok"] = [[r[0], int(r[1])] for r in c.fetchall()]

        c.execute("SELECT to_char(created_at, 'MM-DD HH24'), count(*) FROM analytics.proxy_api_calls WHERE created_at >= CURRENT_DATE - 1 GROUP BY 1 ORDER BY 1")
        rows = c.fetchall()
        data["trend_labels"] = [r[0] for r in rows]
        data["trend_data"] = [r[1] for r in rows]

        c.execute("SELECT model, count(*) FROM analytics.proxy_api_calls WHERE created_at >= CURRENT_DATE - 7 GROUP BY model ORDER BY count(*) DESC")
        rows = c.fetchall()
        data["model_labels"] = [r[0] for r in rows]
        data["model_data"] = [r[1] for r in rows]

        # 话题分布（7天）
        c.execute("SELECT topic, count(*) FROM analytics.proxy_api_calls WHERE created_at >= CURRENT_DATE - 7 AND topic != '' GROUP BY topic ORDER BY count(*) DESC")
        rows = c.fetchall()
        data["topic_labels"] = [r[0] for r in rows]
        data["topic_data"] = [r[1] for r in rows]

        return data
    finally:
        put_conn(conn)
