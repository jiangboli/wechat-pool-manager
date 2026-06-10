from fastapi import APIRouter
from database import get_conn, put_conn

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard")
async def get_dashboard():
    conn = get_conn()
    try:
        c = conn.cursor()
        data = {}

        c.execute("SELECT count(*), COALESCE(SUM(total_tokens),0), count(*) FILTER (WHERE error_type != ''), COALESCE(round(AVG(latency_ms)),0) FROM analytics.proxy_api_calls WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'")
        r = c.fetchone()
        data["today_calls"] = r[0]
        data["today_tokens"] = r[1]
        data["today_errors"] = r[2]
        data["avg_latency"] = r[3]

        c.execute("SELECT count(DISTINCT user_id) FROM analytics.proxy_api_calls WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai' - INTERVAL '7 days'")
        data["active_users_7d"] = c.fetchone()[0]

        c.execute("SELECT count(*) FROM public.bindings WHERE status != 'unbound'")
        data["total_users"] = c.fetchone()[0]

        c.execute("SELECT count(*) FROM public.bindings WHERE bound_at >= date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai' AND status != 'unbound'")
        data["today_new_users"] = c.fetchone()[0]

        c.execute("SELECT COALESCE(NULLIF(username,''), user_id) AS display_name, COALESCE(SUM(total_tokens),0) FROM analytics.proxy_api_calls WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai' GROUP BY display_name ORDER BY SUM(total_tokens) DESC LIMIT 10")
        data["top_req"] = [[r[0], int(r[1])] for r in c.fetchall()]

        c.execute("SELECT COALESCE(NULLIF(username,''), user_id) AS display_name, COALESCE(SUM(total_tokens),0) FROM analytics.proxy_api_calls WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai' - INTERVAL '7 days' GROUP BY display_name ORDER BY SUM(total_tokens) DESC LIMIT 10")
        data["top_tok"] = [[r[0], int(r[1])] for r in c.fetchall()]

        c.execute("SELECT to_char(created_at + INTERVAL '8 hours', 'MM-DD HH24'), count(*) FROM analytics.proxy_api_calls WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai' - INTERVAL '1 day' GROUP BY 1 ORDER BY 1")
        rows = c.fetchall()
        data["trend_labels"] = [r[0] for r in rows]
        data["trend_data"] = [r[1] for r in rows]

        c.execute("SELECT to_char(created_at + INTERVAL '8 hours', 'MM-DD'), COALESCE(SUM(total_tokens), 0) FROM analytics.proxy_api_calls WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai' - INTERVAL '30 days' GROUP BY 1 ORDER BY 1")
        rows = c.fetchall()
        data["token_trend_labels"] = [r[0] for r in rows]
        data["token_trend_data"] = [int(r[1]) for r in rows]

        # 话题分布（7天）
        c.execute("SELECT topic, count(*) FROM analytics.proxy_api_calls WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai' - INTERVAL '7 days' AND topic != '' GROUP BY topic ORDER BY count(*) DESC")
        rows = c.fetchall()
        data["topic_labels"] = [r[0] for r in rows]
        data["topic_data"] = [r[1] for r in rows]

        # 余额快照 - 每个 label 最新余额 + 消耗计算
        c.execute("""
            SELECT DISTINCT ON (label) label, balance, snapshot_at
            FROM analytics.balance_snapshots
            ORDER BY label, snapshot_at DESC
        """)
        latest_rows = {r[0]: {"balance": float(r[1]), "snapshot_at": r[2].isoformat()} for r in c.fetchall()}

        c.execute("""
            SELECT DISTINCT ON (label) label, balance
            FROM analytics.balance_snapshots
            WHERE snapshot_at < date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'
            ORDER BY label, snapshot_at DESC
        """)
        yesterday_latest = {r[0]: float(r[1]) for r in c.fetchall()}

        c.execute("""
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
