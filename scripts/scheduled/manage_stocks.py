#!/usr/bin/env python3
"""股票提醒管理工具
用法:
  python3 manage_stocks.py add <代码> <名称>     # 添加股票，自动更新wrapper+cron
  python3 manage_stocks.py remove <代码>         # 移除股票
  python3 manage_stocks.py list                 # 列出所有
  python3 manage_stocks.py sync                 # 重新生成wrapper+修复cron
"""
import sys, json, os

HERMES_HOME = os.path.expanduser("~/.hermes")
CONFIG_FILE = os.path.join(HERMES_HOME, "scripts/scheduled/stocks.json")
WRAPPER_FILE = os.path.join(HERMES_HOME, "scripts/scheduled/stocks_batch.sh")
BATCH_SCRIPT = os.path.join(HERMES_HOME, "scripts/scheduled/stocks_batch.py")


def load_stocks():
    if not os.path.exists(CONFIG_FILE):
        return []
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)


def save_stocks(stocks):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(stocks, f, ensure_ascii=False, indent=2)


def generate_wrapper(stocks):
    """生成 bash wrapper — 调用 stocks_batch.py"""
    if not stocks:
        return
    args = " ".join(f"{s['code']}:{s['name']}" for s in stocks)
    wrapper = f"""#!/bin/bash
cd {HERMES_HOME}
python3 scripts/scheduled/stocks_batch.py {args}
"""
    with open(WRAPPER_FILE, 'w') as f:
        f.write(wrapper)
    os.chmod(WRAPPER_FILE, 0o755)
    print(f"✅ wrapper 已更新：{len(stocks)} 只股票")


def fix_cron_job(stocks):
    """修复或创建 cron job"""
    jobs_path = os.path.join(HERMES_HOME, "cron/jobs.json")
    if not os.path.exists(jobs_path):
        print("⚠️ jobs.json 不存在，跳过 cron 修复")
        return

    with open(jobs_path, 'r') as f:
        data = json.load(f)

    BATCH_JOB_ID = "batch_stocks_001"

    # 删除所有旧的独立股票 cron job
    removed = 0
    new_jobs = []
    for j in data['jobs']:
        name = j.get('name', '')
        if '股价播报' in name and j.get('id') != BATCH_JOB_ID:
            removed += 1
            continue
        new_jobs.append(j)
    data['jobs'] = new_jobs

    # 查找或创建批量 job
    batch_job = None
    for j in data['jobs']:
        if j.get('id') == BATCH_JOB_ID:
            batch_job = j
            break

    if batch_job is None:
        batch_job = {}
        data['jobs'].append(batch_job)

    batch_job.update({
        'id': BATCH_JOB_ID,
        'name': '股票批量播报',
        'script': 'scheduled/stocks_batch.sh',
        'no_agent': True,
        'schedule': {'kind': 'cron', 'expr': '*/5 9-11,13-15 * * 1-5',
                     'display': '*/5 9-11,13-15 * * 1-5'},
        'deliver': 'origin',
        'enabled': True,
        'repeat': 'forever'
    })

    with open(jobs_path, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"✅ cron 已修复：删除 {removed} 个旧 job，批量 job 已就绪")


def cmd_add(code, name):
    stocks = load_stocks()
    # 避免重复
    existing = [s for s in stocks if s['code'] == code]
    if existing:
        print(f"⚠️ {name}({code}) 已存在，跳过")
        return
    stocks.append({"code": code, "name": name})
    save_stocks(stocks)
    generate_wrapper(stocks)
    fix_cron_job(stocks)
    print(f"✅ 已添加 {name}({code})，当前共 {len(stocks)} 只股票")


def cmd_remove(code):
    stocks = load_stocks()
    new_stocks = [s for s in stocks if s['code'] != code]
    if len(new_stocks) == len(stocks):
        print(f"⚠️ 未找到 {code}")
        return
    save_stocks(new_stocks)
    generate_wrapper(new_stocks)
    fix_cron_job(new_stocks)
    print(f"✅ 已移除 {code}，剩余 {len(new_stocks)} 只")


def cmd_list():
    stocks = load_stocks()
    if not stocks:
        print("暂无股票")
        return
    for i, s in enumerate(stocks, 1):
        print(f"  {i}. {s['name']} ({s['code']})")


def cmd_sync():
    stocks = load_stocks()
    if not stocks:
        print("⚠️ 暂无股票数据")
        return
    generate_wrapper(stocks)
    fix_cron_job(stocks)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "add" and len(sys.argv) >= 4:
        cmd_add(sys.argv[2], sys.argv[3])
    elif cmd == "remove" and len(sys.argv) >= 3:
        cmd_remove(sys.argv[2])
    elif cmd == "list":
        cmd_list()
    elif cmd == "sync":
        cmd_sync()
    else:
        print(__doc__)
