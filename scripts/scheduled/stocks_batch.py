#!/usr/bin/env python3
"""批量股票实时价查询
用法: python3 stocks_batch.py sh600519:贵州茅台 sz002798:帝欧水华 sz300051:琏升科技
"""
import sys, urllib.request
from datetime import datetime

if len(sys.argv) < 2:
    print("用法: stocks_batch.py <代码:名称> [<代码:名称> ...]")
    sys.exit(1)

now = datetime.now().strftime("%H:%M")
output_lines = []

for arg in sys.argv[1:]:
    parts = arg.split(":", 1)
    if len(parts) != 2:
        continue
    code, name = parts[0].strip(), parts[1].strip()
    url = f"http://qt.gtimg.cn/q={code}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("gbk")

        if not raw or "none" in raw.lower():
            output_lines.append(f"📊 **{name}**({code[2:]}) — 暂无数据")
            continue

        data = raw.split("=", 1)[-1].strip().strip('\";')
        fields = data.split("~")

        price = float(fields[3])
        change_pct = float(fields[31])
        change_amt = float(fields[32])
        high = float(fields[33])
        low = float(fields[34])
        volume = float(fields[6])
        turnover = float(fields[37])
        swap_rate = float(fields[38])

        arrow = "🔺" if change_pct > 0 else ("🔻" if change_pct < 0 else "➖")

        output_lines.append(
            f"📊 **{name}**({code[2:]})  ¥{price:.2f}  {arrow}{change_pct:+.2f}%"
            f"  最高¥{high:.2f}  最低¥{low:.2f}"
            f"  成交{volume/10000:.0f}万手  ¥{turnover/10000:.2f}亿"
        )

    except Exception as e:
        output_lines.append(f"📊 **{name}**({code[2:]}) — 查询失败")

output_lines.append(f"⏰ {now}")
print("\n".join(output_lines))
