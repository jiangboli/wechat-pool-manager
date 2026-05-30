---
name: wechat-binding-dedup-phone
审核状态: 待审核
审核人:
审核时间:
---

# 微信绑定去重 & 手机号防重复方案

## 1. 绑定流程（现状）

```
用户打开绑定页 → 填表单(手机号/用户名/龙虾名) → POST /api/v1/bind/register
    ↓
后端分配热池槽位 → 返回 slot_id + qr_url + pending_token
    ↓
前端展示二维码 → 用户微信扫码确认
    ↓
HotPoolSlot.run() 轮询 QR 状态 → 收到 confirmed
    ↓
_on_confirmed 回调（当前有去重逻辑但查的是内存 state → 无效）
    ↓
_run_slot 继续：
  ① create_container() — 创建 Docker bot 容器
  ② create_binding() — 写入 PG bindings 表
```

### 1.1 当前绑定记录表结构

```sql
bindings (
    id              SERIAL PRIMARY KEY,
    profile_name    VARCHAR(32) UNIQUE NOT NULL,    -- 配置名，如 weixin-003
    machine_ip      VARCHAR(45) NOT NULL,           -- 运行此容器的服务器 IP
    user_id         VARCHAR(128),                   -- 微信用户 ID (ilink_user_id)【全局一致】
    account_id      VARCHAR(128),                   -- 机器人账号 ID (ilink_bot_id)【每个机器人不同】
    phone           VARCHAR(20),                    -- 手机号（表单提交）
    lobster_name    VARCHAR(64),                    -- 龙虾名
    user_name       VARCHAR(64),                    -- 用户名/称呼
    status          VARCHAR(20) DEFAULT 'active',   -- active/inactive/expired
    ...
)
```

### 1.2 关键字段说明

| 字段 | 值 | 是否跨机器人一致 |
|------|----|----------------|
| `user_id` (ilink_user_id) | 微信用户在 iLink 系统的全局 ID | ✅ **一致** |
| `account_id` (ilink_bot_id) | 每个机器人账号单独的 ID | ❌ 每个不一样的机器人不一样 |
| `phone` | 用户扫码前填的手机号 | ✅ 一致（用来去重） |

---

## 2. 问题

### 2.1 同一个微信扫两次 → 创建两个容器

当前去重逻辑（`hot_pool.py:289`）：

```python
existing_profile = self.state.get_docker_user_by_user_id(user_id)
```

查的是 `PoolState` 内存字典（JSON 文件维护），**容器重启后 state 文件是空**，所以永远是"新用户"。

结果：一个用户扫两次码，产生 weixin-004、weixin-005 两个容器，同一微信号服务会串。

### 2.2 跨服务器：`user_id` 全局一致但机器不同

`ilink_user_id` 是微信用户全局 ID，跨机器人/服务器不变。如果一个用户在 Server A 绑过，又在 Server B 扫码：

| 方案 | 行为 |
|------|------|
| **全局去重（跨服务器）** | 报错"你已在其他服务器上绑定" |
| **手机号去重（建议）** | 手机号+本服务器双重检查 |

---

## 3. 方案：手机号去重

### 3.1 原则

1. **同一个手机号不能在同一台服务器上绑定两次**
2. **同一个手机号在不同服务器上可以分别绑定**（独立部署，各管各的）
3. 去重检查在**表单提交阶段**（扫码前）就做，而不是等到扫码后

### 3.2 完整流程

```
用户填写手机号 → POST /api/v1/bind/register
    ↓
检查 PG：phone + machine_ip 是否有 active 绑定？
    ├── ✅ 找到 → 返回 409："该手机号已在当前服务器上绑定了"
    └── ❌ 没找到 → 继续分配热池槽位
         ↓
        用户微信扫码确认
         ↓
        _run_slot 创建容器 + 写入 PG 绑定记录
```

### 3.3 用户扫码后二次去重（安全带）

扫码后 `_on_confirmed` 回调中再次检查 PG（`phone + machine_ip`），防止表单提交后到扫码确认之间产生了并发绑定：

```python
async def _on_confirmed(self, result: dict):
    # 二次去重：phone + machine_ip
    phone = slot.user_info.get("phone", "")
    existing = await pg_store.find_binding_by_phone_and_machine(phone, _MACHINE_IP)
    if existing:
        # 更新凭证 + 重启容器（不是创建新的）
        update_credentials(existing["profile_name"], credentials)
        restart_container(existing["profile_name"])
        return
    # ... 正常创建流程
```

---

## 4. 变更清单

### 4.1 新增：`pg_store.py` — 手机号查绑定

```python
async def find_binding_by_phone_and_machine(
    self, phone: str, machine_ip: str
) -> Optional[dict]:
    """按手机号+机器IP查找active绑定（同服务器去重）。"""
    stmt = select(Binding).where(
        Binding.phone == phone,
        Binding.machine_ip == machine_ip,
        Binding.status == "active",
    )
    ...
```

### 4.2 新增：`pg_store.py` — 绑定表 `phone + machine_ip` 联合唯一约束（可选）

```sql
ALTER TABLE bindings ADD CONSTRAINT uq_phone_machine UNIQUE (phone, machine_ip);
```

或者不建约束，靠应用层检查（推荐，兼容已有数据）。

### 4.3 修改：`service.py` 或 `bind_router.py` — 表单提交时检查

`POST /api/v1/bind/register` 处理函数中，收到手机号后先查 PG：

```python
if pg_store.enabled:
    existing = await pg_store.find_binding_by_phone_and_machine(phone, MACHINE_IP)
    if existing:
        raise HTTPException(409, "该手机号已在当前服务器上绑定了")
```

### 4.4 修改：`hot_pool.py:276` — `_on_confirmed` 二次去重

```python
async def _on_confirmed(self, result: dict):
    user_id = result.get("user_id", "")
    profile = result["profile"]
    slot = self.slots.get(profile)

    # 从 slot.user_info 取手机号（表单提交时暂存的）
    phone = (slot.user_info or {}).get("phone", "")

    # 去重：phone + machine_ip
    if phone and pg_store.enabled:
        existing = await pg_store.find_binding_by_phone_and_machine(phone, _MACHINE_IP)
        if existing:
            logger.info("手机号 %s 已绑定（profile=%s），复用已有容器", phone, existing["profile_name"])
            # 更新凭证
            update_credentials(existing["profile_name"], credentials)
            # 重启容器
            restart_container(existing["profile_name"])
            return

    # 新用户：正常创建流程
    await self._scheduler.create_container(profile, credentials, ...)
    await pg_store.create_binding(profile=profile, phone=phone, ...)
```

---

## 5. 实现步骤

### Step 1: pg_store.py 加 `find_binding_by_phone_and_machine()`

```python
async def find_binding_by_phone_and_machine(self, phone: str, machine_ip: str) -> Optional[dict]:
    if not self._enabled or not phone:
        return None
    try:
        async with self._session() as session:
            stmt = select(Binding).where(
                Binding.phone == phone,
                Binding.machine_ip == machine_ip,
                Binding.status == "active",
            )
            result = await session.execute(stmt)
            binding = result.scalar_one_or_none()
            if binding:
                return {"profile_name": binding.profile_name, "machine_ip": binding.machine_ip, ...}
            return None
    except Exception as e:
        logger.warning("手机号去重查询失败: %s", e)
        return None
```

### Step 2: 表单提交接口加手机号去重检查

找到 `POST /api/v1/bind/register` 路由，在分配热池槽位前检查手机号。

### Step 3: hot_pool.py `_on_confirmed` 改去重逻辑

用 `pg_store.find_binding_by_phone_and_machine()` 替代内存 state 检查。

### Step 4: hot_pool.py `_run_slot` 修复容器创建逻辑

当 `_on_confirmed` 检测到重复并提前返回后，`_run_slot` 不应继续创建容器。

### Step 5: 测试

- 同一手机号填两次表单 → 第一次成功，第二次 409
- 同一手机号扫码一次 → 扫码后成功绑上
- 同一手机号扫两次码（避开表单检查）→ 第二次不创建新容器，只重启旧的
- 不同服务器相同手机号 → 各自绑定，互不影响

---

## 6. 风险

- 手机号不是必填的。如果用户不填手机号，表单检查跳过，靠扫码后二次检查
- 二次检查如果遇到 PG 不可用（pg_store disabled），跳过去重
- 已有数据：已有重复绑定的手机号不会被清理，但新绑定会受约束
