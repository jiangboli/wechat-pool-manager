---
name: lobster-user-info-binding
审核状态: 通过 ✅
审核人: jiangboli (用户)
审核时间: 2026-05-30 02:30
审核意见: "开始执行"
---

# 用户信息收集 + 绑定关联方案

## 目标

绑定页面增加三步流程：**填写信息 → 扫码绑定 → 龙虾名同步到机器人**

## 现状分析

### 当前绑定流程
```
用户打开页面 → 热池槽位显示二维码 → 用户微信扫码 → 确认绑定
                                                    ↓
                                              HotPool._on_confirmed()
                                                    ↓
                                              create_container() → PG 写入
```

### 缺失能力
1. 扫码前零信息收集——不知道是谁绑的
2. Bindings 表无 phone / lobster_name 字段
3. Hermes bot 容器名称固定为 `weixin-NNN`，无个性化名

## 方案设计

### 新增流程

```
用户打开页面
    ↓
┌─────────────────────┐
│ 表单：填写用户信息    │
│ · 手机号（必填）      │
│ · 用户名（必填）      │
│ · 龙虾名（必填）      │
│ [提交并开始绑定]      │
└─────────────────────┘
    ↓  POST /api/v1/bind/register
    ↓  返回 pending_token + slot_id
    ↓
┌─────────────────────┐
│ 展示二维码            │
│ "亲爱的 {龙虾名}，请扫码" │
└─────────────────────┘
    ↓  用户扫码 → 确认
    ↓
PG 写入：
  - bindings 表：phone, lobster_name, user_name 新增字段
  - qr_history 关联 pending_token
    ↓
hermes-bot 容器创建时，config.yaml 中写入 bot_name = lobster_name
```

### 数据模型变更

**bindings 表新增字段：**

```sql
ALTER TABLE bindings ADD COLUMN phone VARCHAR(20) COMMENT '手机号';
ALTER TABLE bindings ADD COLUMN lobster_name VARCHAR(64) COMMENT '龙虾名（用作机器人名）';
ALTER TABLE bindings ADD COLUMN user_name VARCHAR(64) COMMENT '用户名/称呼';
```

**ORM 模型变更（models.py）：**

```python
class Binding(Base):
    # ... 现有字段不变
    phone = Column(String(20), comment="手机号")
    lobster_name = Column(String(64), comment="龙虾名（用作机器人名）")
    user_name = Column(String(64), comment="用户名/称呼")
```

### API 变更

#### 新增：POST /api/v1/bind/register

```json
// Request
{
  "phone": "13800138000",
  "user_name": "张三",
  "lobster_name": "红魔虾"
}

// Response
{
  "pending_token": "tok_xxxx",
  "slot_id": "weixin-005",
  "qr_url": "https://..."
}
```

行为：
1. 分配一个可用热池槽位
2. 将用户信息存入 PgStore 的 pending_bindings 表（或内存 dict + PG temp）
3. 返回 slot_id 和 qr_url
4. 前端切换到扫码页面

#### 修改：HotPool._on_confirmed()

绑定成功后，将 pending 的用户信息同步到 bindings 表：
- 写入 phone, lobster_name, user_name
- 清除 pending 记录

### 前端变更（index.html）

新增一个表单页面模块，替换当前直接显示二维码的流程：

```
# 页面状态 1: 表单
- 三个输入框（手机号/用户名/龙虾名）
- 每个输入框下方显示验证提示
- 提交按钮：开始绑定
- 表单验证：全部必填，手机号格式校验

# 页面状态 2: 扫码
- 显示 "欢迎 {龙虾名}！请扫码绑定"
- 显示二维码
- 轮询绑定状态
```

设计风格保持现有龙虾主题（dosh.png 大龙虾）。

### Hermes 机器人名同步

在 `docker_scheduler.py` 的 `write_config()` 中，新增 `bot_name` 字段写入 config.yaml：

```yaml
# config.yaml 模板新增
agent:
  persona: |
    You are a helpful assistant named {lobster_name}.
    你的名字是{lobster_name}，用中文回答。
```

或者在创建容器时通过环境变量 `HERMES_BOT_NAME` 传入，bot 启动时自动读取。

由于 Hermes gateway 的 agent persona 配置文件是固定的，最可靠的方式是：

1. `docker_scheduler.py` 创建容器时，将 lobster_name 写入容器的 `.env` 文件
2. 在 `Dockerfile.bot` 的 `startup.py` 中读取该环境变量并设置 agent persona

### 变更清单

| 文件 | 改动 |
|------|------|
| `pool_manager/models.py` | Binding 表新增 phone, lobster_name, user_name 字段 |
| `pool_manager/pg_store.py` | 新增 `create_pending_binding()`、`get_pending_binding()`、`confirm_binding()` 方法；修改 create_binding() 接受 user_info 参数 |
| `pool_manager/service.py` | 新增 `POST /api/v1/bind/register` 端点；修改绑定完成后的回调逻辑 |
| `pool_manager/hot_pool.py` | `_on_confirmed()` 接受 pending_token 参数，绑定完成后写入 user_info |
| `pool_manager/docker_scheduler.py` | `create_container()` 接受 lobster_name 参数，写入 bot 配置 |
| `Dockerfile.bot` / `startup.py` | 读取 lobster_name 环境变量，设置 agent persona |
| `static/index.html` | 新增表单页面 + 流程切换 |
| `scripts/setup.sh` | 新增 `--machine-ip` 参数（已有） |

### 实现步骤

1. **后端数据模型** — models.py 加字段 + pg_store.py 加 pending 方法
2. **API 端点** — service.py 新增 POST /api/v1/bind/register
3. **绑定回调** — hot_pool.py 修改 _on_confirmed() 传递 user_info 到 create_binding()
4. **前端表单** — index.html 新增表单 UI + JS 流程控制
5. **Hermes 机器人名** — docker_scheduler.py 传递 lobster_name + startup.py 读取
6. **自测试** — 编译检查 + 全流程验证
7. **构建部署** — PR → 合并 → SCP → 重建容器

### 验证方式

1. 打开绑定页，确认显示表单而非直接二维码
2. 不填信息提交 → 提示必填
3. 填完信息提交 → 显示二维码，标题含龙虾名
4. 扫码绑定成功 → PG bindings 表含 phone/lobster_name/user_name
5. 查看 bot 容器日志，确认 lobster_name 出现在 persona 中

### 风险与注意事项

- **HotPool 与用户信息的状态关联** — HotPool 是异步轮询的，用户填完信息后要锁定一个槽位给该用户，不可被其他用户抢走
- **PG 不可用降级** — PgStore 降级时，用户信息写入内存 dict，绑定完成后尝试写入 PG
- **已有绑定不影响** — 已有的 bindings 记录 phone/lobster_name/user_name 为空，不影响现有功能
- **前端刷新** — 表单提交流程走的 SPA 模式，页面刷新会导致 pending 丢失，用户需重新填写
