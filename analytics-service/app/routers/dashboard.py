from fastapi import APIRouter
from database import get_conn, put_conn

router = APIRouter(tags=["dashboard"])

BJ_START = "date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'"


@router.get("/dashboard")
async def get_dashboard():
    conn = get_conn()
    try:
        c = conn.cursor()
        data = {}

        # ── 今日统计 ──
        c.execute(f"SELECT count(*), COALESCE(SUM(total_tokens),0), count(*) FILTER (WHERE error_type != ''), COALESCE(round(AVG(latency_ms)),0) FROM analytics.proxy_api_calls WHERE created_at >= {BJ_START}")
        r = c.fetchone()
        data["today_calls"] = r[0]
        data["today_tokens"] = r[1]
        data["today_errors"] = r[2]
        data["avg_latency"] = r[3]

        # ── DAU（今日 + 30天趋势）──
        c.execute(f"SELECT count(DISTINCT user_id) FROM analytics.proxy_api_calls WHERE created_at >= {BJ_START}")
        data["dau_today"] = int(c.fetchone()[0])

        c.execute(f"SELECT to_char(created_at + INTERVAL '8 hours', 'MM-DD'), count(DISTINCT user_id) FROM analytics.proxy_api_calls WHERE created_at >= {BJ_START} - INTERVAL '30 days' GROUP BY 1 ORDER BY 1")
        rows = c.fetchall()
        data["dau_labels"] = [r[0] for r in rows]
        data["dau_data"] = [r[1] for r in rows]

        # ── 人均指标 ──
        dau = data["dau_today"]
        data["per_user_calls"] = round(data["today_calls"] / dau, 1) if dau else 0
        data["per_user_tokens"] = int(round(data["today_tokens"] / dau, 0)) if dau else 0

        # ── 7天活跃 ──
        c.execute(f"SELECT count(DISTINCT user_id) FROM analytics.proxy_api_calls WHERE created_at >= {BJ_START} - INTERVAL '7 days'")
        data["active_users_7d"] = c.fetchone()[0]

        # ── 总用户 / 今日新增 ──
        c.execute("SELECT count(*) FROM public.bindings WHERE status != 'unbound'")
        data["total_users"] = c.fetchone()[0]

        c.execute(f"SELECT count(*) FROM public.bindings WHERE bound_at >= {BJ_START} AND status != 'unbound'")
        data["today_new_users"] = c.fetchone()[0]

        # ── Token排行 ──
        c.execute(f"SELECT COALESCE(NULLIF(username,''), user_id) AS display_name, COALESCE(SUM(total_tokens),0) FROM analytics.proxy_api_calls WHERE created_at >= {BJ_START} GROUP BY display_name ORDER BY SUM(total_tokens) DESC LIMIT 10")
        data["top_req"] = [[r[0], int(r[1])] for r in c.fetchall()]

        c.execute(f"SELECT COALESCE(NULLIF(username,''), user_id) AS display_name, COALESCE(SUM(total_tokens),0) FROM analytics.proxy_api_calls WHERE created_at >= {BJ_START} - INTERVAL '7 days' GROUP BY display_name ORDER BY SUM(total_tokens) DESC LIMIT 10")
        data["top_tok"] = [[r[0], int(r[1])] for r in c.fetchall()]

        # ── 小时热力图（7天，北京时）──
        c.execute(f"SELECT to_char(created_at + INTERVAL '8 hours', 'MM-DD') as day, extract(hour from created_at + INTERVAL '8 hours')::int as hour, count(*) FROM analytics.proxy_api_calls WHERE created_at >= {BJ_START} - INTERVAL '7 days' GROUP BY 1, 2 ORDER BY 1, 2")
        rows = c.fetchall()
        data["heatmap"] = [[r[0], r[1], r[2]] for r in rows]

        # ── 30天Token趋势 ──
        c.execute(f"SELECT to_char(created_at + INTERVAL '8 hours', 'MM-DD'), COALESCE(SUM(total_tokens), 0) FROM analytics.proxy_api_calls WHERE created_at >= {BJ_START} - INTERVAL '30 days' GROUP BY 1 ORDER BY 1")
        rows = c.fetchall()
        data["token_trend_labels"] = [r[0] for r in rows]
        data["token_trend_data"] = [int(r[1]) for r in rows]

        # ── 话题分布（7天）──
        c.execute(f"SELECT topic, count(*) FROM analytics.proxy_api_calls WHERE created_at >= {BJ_START} - INTERVAL '7 days' AND topic != '' GROUP BY topic ORDER BY count(*) DESC")
        rows = c.fetchall()
        data["topic_labels"] = [r[0] for r in rows]
        data["topic_data"] = [r[1] for r in rows]

        # ── 容器健康分布 ──
        c.execute("""
            SELECT 
              CASE 
                WHEN b.status = 'session_expired' THEN 'session_expired'
                WHEN b.last_heartbeat_at IS NULL THEN 'dead'
                WHEN b.last_heartbeat_at >= now() - INTERVAL '10 minutes' THEN 'healthy'
                WHEN b.last_heartbeat_at >= now() - INTERVAL '30 minutes' THEN 'warning'
                ELSE 'dead'
              END AS health_status,
              count(*)
            FROM public.bindings b
            WHERE b.status != 'unbound'
            GROUP BY 1
        """)
        rows = c.fetchall()
        data["health_labels"] = [r[0] for r in rows]
        data["health_data"] = [r[1] for r in rows]

        # ── 余额快照 ──
        c.execute("""
            SELECT DISTINCT ON (label) label, balance, snapshot_at
            FROM analytics.balance_snapshots
            ORDER BY label, snapshot_at DESC
        """)
        latest_rows = {r[0]: {"balance": float(r[1]), "snapshot_at": r[2].isoformat()} for r in c.fetchall()}

        # 昨日基线算今日消耗
        c.execute(f"""
            SELECT DISTINCT ON (label) label, balance
            FROM analytics.balance_snapshots
            WHERE snapshot_at < {BJ_START}
            ORDER BY label, snapshot_at DESC
        """)
        yesterday_latest = {r[0]: float(r[1]) for r in c.fetchall()}

        c.execute(f"""
            SELECT DISTINCT ON (label) label, balance
            FROM analytics.balance_snapshots
            WHERE snapshot_at >= date_trunc('month', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'
            ORDER BY label, snapshot_at ASC
        """)
        month_first = {r[0]: float(r[1]) for r in c.fetchall()}

        if latest_rows:
            total_balance = 0.0
            total_today = 0.0
            total_month = 0.0
            balances_list = []
            for label, info in sorted(latest_rows.items()):
                bal = info["balance"]
                today_cost = max(0.0, round((yesterday_latest.get(label, bal) - bal), 2))
                month_cost = max(0.0, round((month_first.get(label, bal) - bal), 2))
                total_balance += bal
                total_today += today_cost
                total_month += month_cost
                balances_list.append({"label": label, "balance": round(bal, 2), "today_cost": today_cost, "month_cost": month_cost})
            data["total_balance"] = round(total_balance, 2)
            data["today_cost"] = round(total_today, 2)
            data["month_cost"] = round(total_month, 2)
            data["balances"] = balances_list
        else:
            data["total_balance"] = None
            data["today_cost"] = None
            data["month_cost"] = None
            data["balances"] = []

        return data
    finally:
        put_conn(conn)
