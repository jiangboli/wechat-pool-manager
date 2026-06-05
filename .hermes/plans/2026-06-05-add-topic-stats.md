---
name: add-topic-stats-api
审核状态: 通过 ✅
审核人: jreye (用户)
审核时间: 2026-06-05
审核意见: "继续执行"
---

# 看板增加按主题统计

## 目标
在管理端增加话题分类统计，展示所有用户的 LLM 调用按 topic 的分布情况。

## 变更

### 1. `pool_manager/service.py` — 新增 API

在 `admin_app` 上新增 `/api/v1/analytics/topics` 端点：

```python
@admin_app.get("/api/v1/analytics/topics")
async def get_topic_stats(days: int = 7):
    """返回话题分类分布统计。"""
    # 查询 PG analytics.proxy_api_calls 表
    # 按 topic 分组，统计数量、占比
    # 返回 [{topic: "科技互联网", count: 487, pct: 32.5}, ...]
```

### 2. 依赖

- `admin_app` 已有 `CLAW_DO_DSN` 环境变量（通过 pg_store）
- 直接用 `psycopg2` 查询 `analytics` schema（在函数内 import）

## 返回格式

```json
{
  "total": 1500,
  "period": {"start": "2026-05-29", "end": "2026-06-05"},
  "topics": [
    {"topic": "科技互联网", "emoji": "💻", "count": 487, "pct": 32.5},
    {"topic": "财经金融", "emoji": "📊", "count": 380, "pct": 25.3},
    ...
  ]
}
```

## 自测试

- 语法检查 (`py_compile`)
- API curl 验证

## 部署

- PR → 合并到 main
- 取最新代码到 Server-87 构建 Docker 镜像
- 传镜像到 dosh 重启
