#!/usr/bin/env python3
"""Claw DO Analytics ETL v2 - daily data collection from sessions metadata + memories + logs."""
import asyncio, asyncpg, os, json, glob, hashlib, re
from datetime import datetime, timezone, date, timedelta
from collections import defaultdict

DATA_ROOT = "/home/dosh/data/"
NOW = datetime.now(timezone.utc)
TODAY = NOW.date()
DSN = os.environ.get('CLAW_DO_DSN', '').replace('+asyncpg', '')

async def main():
    conn = await asyncpg.connect(DSN)
    profiles = []
    for d in sorted(glob.glob(DATA_ROOT + '[0-9]/*/')):
        prof = d.rstrip('/').split('/')[-1]
        hermes_dir = d + '.hermes/'
        if os.path.exists(hermes_dir + 'sessions/sessions.json'):
            profiles.append((prof, hermes_dir, d))

    print(f"Found {len(profiles)} profiles")

    for prof, hermes_dir, data_dir in profiles:
        await process_profile(conn, prof, hermes_dir)

    await update_weekly_stats(conn)
    await update_user_profiles(conn)
    await conn.close()
    print("ETL completed")

async def process_profile(conn, prof, hermes_dir):
    sessions_path = hermes_dir + 'sessions/sessions.json'
    if not os.path.exists(sessions_path):
        return

    try:
        with open(sessions_path) as f:
            sessions_data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return

    if not isinstance(sessions_data, dict):
        return

    user_md_text = safe_read(hermes_dir + 'memories/USER.md')
    memory_md_text = safe_read(hermes_dir + 'memories/MEMORY.md')

    # Parse user_name from USER.md
    user_name = ''
    for line in user_md_text.split('\n'):
        line = line.strip()
        if line.startswith('user_name:'):
            user_name = line.split(':', 1)[1].strip()
        elif line.startswith('name:'):
            user_name = line.split(':', 1)[1].strip()
    if not user_name:
        user_name = prof

    today_sessions = 0
    today_tokens = 0
    today_cost = 0.0
    today_resets = 0
    topics_set = set()

    for channel_key, session in sessions_data.items():
        if not isinstance(session, dict):
            continue

        created = parse_time(session.get('created_at'))
        updated = parse_time(session.get('updated_at'))
        sid = session.get('session_id', '')

        input_tokens = session.get('input_tokens', 0) or 0
        output_tokens = session.get('output_tokens', 0) or 0
        total_tokens = session.get('total_tokens', 0) or (input_tokens + output_tokens)
        cost = session.get('estimated_cost_usd', 0) or 0.0
        was_reset = session.get('was_auto_reset', False) or session.get('resume_pending', False)

        # Count today's sessions
        if created and created.date() == TODAY:
            today_sessions += 1
            today_tokens += total_tokens
            today_cost += cost
            if was_reset:
                today_resets += 1

        # Insert conversation (session) metadata
        if created:
            try:
                await conn.execute("""
                    INSERT INTO analytics.conversations
                    (profile_name, user_name, session_id, started_at, ended_at,
                     turn_count, user_msg_count, bot_msg_count, user_msg_total_len,
                     topics, sentiment, skills_used, had_error)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                """, prof, user_name, sid, created, updated,
                    0, 0, 0, 0,
                    None, 'neutral', None, was_reset or False)
            except Exception:
                pass

        # Extract topics from channel key (sometimes has user info)
        if '@im.wechat' in channel_key:
            topics_set.add('wechat')

    # Snapshot memory
    for mem_type, text in [('USER', user_md_text), ('MEMORY', memory_md_text)]:
        if text:
            h = hashlib.md5(text.encode()).hexdigest()[:16]
            existing = await conn.fetchval(
                "SELECT id FROM analytics.memory_evolution WHERE profile_name=$1 AND snapshot_date=$2 AND memory_type=$3",
                prof, TODAY, mem_type)
            if not existing:
                await conn.execute("""
                    INSERT INTO analytics.memory_evolution
                    (profile_name, snapshot_date, memory_type, line_count, content_hash)
                    VALUES ($1,$2,$3,$4,$5)
                """, prof, TODAY, mem_type, len(text.split('\n')), h)

    # Daily stats
    await conn.execute("""
        INSERT INTO analytics.user_daily_stats
        (profile_name, user_name, date, conversation_count, total_turns, total_messages,
         skill_usage, topics, memory_size_bytes, memory_lines, is_active)
        VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11)
        ON CONFLICT (profile_name, date) DO UPDATE SET
            conversation_count = EXCLUDED.conversation_count,
            total_messages = EXCLUDED.total_messages,
            skill_usage = EXCLUDED.skill_usage,
            is_active = EXCLUDED.is_active
    """, prof, user_name, TODAY, today_sessions, today_tokens, today_tokens,
        '{}', list(topics_set) if topics_set else None,
        len(user_md_text) + len(memory_md_text),
        len(user_md_text.split('\n')) + len(memory_md_text.split('\n')),
        today_sessions > 0)

async def update_weekly_stats(conn):
    week_start = TODAY - timedelta(days=TODAY.weekday())
    existing = await conn.fetchval(
        "SELECT week_start FROM analytics.platform_weekly_stats WHERE week_start=$1", week_start)
    if existing:
        return
    active = await conn.fetchval(
        "SELECT COUNT(DISTINCT profile_name) FROM analytics.user_daily_stats WHERE date >= $1 AND is_active=true", week_start) or 0
    first_seen = await conn.fetchval(
        "SELECT COUNT(*) FROM analytics.user_profiles WHERE first_seen_date >= $1", week_start) or 0
    await conn.execute("""
        INSERT INTO analytics.platform_weekly_stats
        (week_start, active_users, new_users, total_conversations, total_messages)
        VALUES ($1,$2,$3,
            (SELECT COALESCE(SUM(conversation_count),0) FROM analytics.user_daily_stats WHERE date >= $1),
            (SELECT COALESCE(SUM(total_messages),0) FROM analytics.user_daily_stats WHERE date >= $1))
    """, week_start, active, first_seen)

async def update_user_profiles(conn):
    rows = await conn.fetch("""
        SELECT profile_name, user_name,
            MIN(date) as first_seen,
            MAX(date) as last_active,
            COUNT(*) as active_days,
            SUM(conversation_count) as total_convos,
            SUM(total_messages) as total_msgs
        FROM analytics.user_daily_stats
        WHERE is_active=true
        GROUP BY profile_name, user_name
    """)
    for r in rows:
        avg_daily = r['total_msgs'] / max(r['active_days'], 1) if r['total_msgs'] else 0
        await conn.execute("""
            INSERT INTO analytics.user_profiles
            (profile_name, user_name, first_seen_date, last_active_date,
             total_conversations, total_messages, avg_daily_messages, is_bound)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (profile_name) DO UPDATE SET
                user_name = EXCLUDED.user_name,
                last_active_date = EXCLUDED.last_active_date,
                total_conversations = EXCLUDED.total_conversations,
                total_messages = EXCLUDED.total_messages,
                avg_daily_messages = EXCLUDED.avg_daily_messages,
                is_bound = EXCLUDED.is_bound,
                updated_at = NOW()
        """, r['profile_name'], r['user_name'] or '', r['first_seen'],
            r['last_active'], r['total_convos'] or 0, r['total_msgs'] or 0,
            round(avg_daily, 1), True)

def safe_read(path):
    try:
        with open(path) as f:
            return f.read()
    except (IOError, FileNotFoundError):
        return ''

def parse_time(ts):
    if not ts:
        return None
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    return ts

if __name__ == '__main__':
    asyncio.run(main())
