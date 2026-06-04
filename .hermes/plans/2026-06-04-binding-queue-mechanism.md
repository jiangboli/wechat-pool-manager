---
name: binding-queue-mechanism
审核状态: 通过 ✅
审核人: jiangboli (用户)
审核时间: 2026-06-04
审核意见: "热池槽位修改为20个，同时按这个计划文件执行"
---

# 热池高峰期排队机制 开发计划

## 目标

解决热池 5 个槽位全部被占用时新用户直接 503 的问题。改为排队等待，有槽位时自动分配。

## 现状

- 热池有 `hot_pool_size=5` 个槽位，每个槽位展示一个 QR 码等待扫码
- 用户注册时搜索 `status=="waiting"` 的槽位，取第一个
- 如果 5 个槽位全部被绑定了用户信息（等待扫码中），新注册返回 503
- `_tick()` 定期补充新槽位，但高峰期速度跟不上

## 方案

在 register 和 hot_pool 之间加一个队列层：

```
用户注册 → 有闲 slot？→ 是 → 分配 slot（现有流程）
                 ↓ 否
            加入排队队列
                 ↓
    (后台任务每 5s 运行)
    _tick() 新 slot 就绪 → 检查队列 → 分配给队首用户
    slot 绑定完成（slot 释放）→ 检查队列 → 分配给队首用户
```

## 变更清单

### 1. `pool_manager/service.py`

**新增：**
- `_binding_queue: asyncio.Queue` 异步队列（或简单用 `List[dict]` + 互斥控制）
- `_assign_from_queue()` — 从队列取第一个人，找可用 slot，分配给他

**修改：**
- `register_binding()` line 170-171: 503 → 加入队列，返回 `{"queued": true, "pending_token": "...", "position": N}`
- `get_rebind_status()`: 处理 queued 状态，返回 `{"status": "queued", "position": N}`

**新增后台任务：**
- `_queue_check_loop()` — 每 5 秒运行一次，检查队列 + 可用 slot

### 2. `static/index.html`

**修改 `submitForm()` line 741-742:**
- 如果 `data.queued` → 显示排队页面，`startPollingRebindStatus(data.pending_token)`

**修改 `pollRebindStatus()`:**
- 处理 `data.status === 'queued'` → 显示排队中提示

**新增：**
- 排队页面元素（等待提示、排队位置、预计等待时间）

### 3. `pool_manager/hot_pool.py`

修改 `_start_slot()` 和 `_run_slot()` 完成后：
- 当 `_run_slot` 正常结束（绑定成功或超时），slot 从 `self.slots` 中移除
- 移除后调用 `_assign_from_queue()`（通过回调或全局引用）

实际上最干净的方案是不改 hot_pool.py，在 service.py 中用一个独立的背景协程定期检查队列。

## 实现步骤

1. 修改 `service.py`：添加队列 + 分配函数 + 后台任务
2. 修改 `service.py`：register_binding 改 503 为排队
3. 修改 `index.html`：排队 UI + pollRebindStatus 处理
4. docker compose 重启 pool-bind 容器测试

## 风险

- 排队数据在内存中，容器重启丢失（可接受，503 也会丢）
- 高并发下队列操作需加锁（但 FastAPI 单线程 + asyncio 不需要显式锁）
- 队列位置不准：多个 slot 同时释放时，队列可能一下子清空，位置数字不精确（可接受，仅展示参考）

## 验证方式

1. 起 10 个浏览器同时注册，前 5 个拿到 QR，后 5 个进排队
2. 前 5 个中有一个扫码完成 → 检查排队中的第一个是否自动拿到了 QR
3. 排队页面显示排队位置信息
