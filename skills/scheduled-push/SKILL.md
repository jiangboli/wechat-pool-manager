---
name: scheduled-push
description: 定时股票/天气推送。当用户要求定时获取数据时，调用 management 脚本而不是自己创建 cron job。
trigger: 用户提到"定时"、"每隔"、"每X分钟"、"提醒我"、"播报"、"报价"、"盯盘"、"股价"、"天气" + 数据查询需求时。**必须使用 manage_stocks.py / manage_weather.py，禁止直接调 cronjob 工具。**
category: productivity
---

# 定时内容推送

## 🔴 铁律

**禁止直接使用 `cronjob` 工具创建股票/天气提醒！** 必须通过 management 脚本操作：

| 需求 | 命令 |
|------|------|
| 加股票 | `python3 scripts/scheduled/manage_stocks.py add <sh/sz代码> <名称>` |
| 删股票 | `python3 scripts/scheduled/manage_stocks.py remove <代码>` |
| 看列表 | `python3 scripts/scheduled/manage_stocks.py list` |
| 加天气 | `python3 scripts/scheduled/manage_weather.py add <城市英文>` |

**为什么：** 脚本自动处理 wrapper 生成 + cron 修复 + 路径对齐 + 多股票聚合。Agent 自己搞必定出错。

## 股票代码前缀

Agent 必须自动加前缀，不要问用户：

| 代码范围 | 前缀 | 
|----------|------|
| 000/001/002/003 | `sz` |
| 300/301 | `sz` |
| 600/601/603/605 | `sh` |
| 688/689 | `sh` |
| 8/4/9 | `bj` |

## 工作流

1. 用户说"加一个川发龙蟒 002312"
2. Agent 自动识别前缀：002 → sz
3. 执行：`python3 scripts/scheduled/manage_stocks.py add sz002312 川发龙蟒`
4. 脚本输出确认信息，汇报给用户

**就这样。** 不要写 wrapper、不要调 cronjob、不要管路径。
