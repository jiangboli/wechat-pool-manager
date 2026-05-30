---
name: wechat-binding-dedup-global-phone
审核状态: 待审核
审核人:
审核时间:
---

# 微信绑定全局手机号去重方案 v2

## 问题

同一手机号（即同一个用户）扫了多次二维码，产生了多个 bot 容器。

## 方案：全局手机号去重

**手机号全局唯一**——不管在哪台服务器上，一个手机号只能绑定一次。

## 绑定流程（改后）

```
用户打开绑定页
    ↓
Step 1：输入手机号 → 点击"验证"
    ↓
GET /api/v1/bind/check-phone?phone=xxx
    ↓
PG 查询该手机号是否有 active 绑定（全局，不限机器）
    ├── ✅ 存在 → 页面显示"该手机号已绑定，不能重复绑定" → 阻止
    └── ❌ 不存在 → 切换到 Step 2
         ↓
Step 2：填写用户名 + 龙虾名 → 点击"开始绑定"
    ↓
POST /api/v1/bind/register（含 phone + user_name + lobster_name）
    ↓
后端再次检查手机号（安全带），同上
    ├── ✅ 存在（并发冲突）→ 409 拒绝
    └── ❌ 不存在 → 分配槽位 → 显示二维码
         ↓
用户扫码确认 → 创建容器 + 写入 PG binding（含 phone）
```

## 变更清单

### 1. pg_store.py — 新增全局手机号查询

```python
async def find_binding_by_phone(self, phone: str) -> Optional[dict]:
    """全局查询：该手机号是否已在任何机器上绑定了。"""
    if not self._enabled or not phone:
        return None
    async with self._session() as session:
        stmt = select(Binding).where(
            Binding.phone == phone,
            Binding.status == "active",
        )
        result = await session.execute(stmt)
        binding = result.scalar_one_or_none()
        if binding:
            return {
                "profile_name": binding.profile_name,
                "machine_ip": binding.machine_ip,
                "bound_at": str(binding.bound_at),
            }
        return None
```

### 2. service.py — 新增 GET /api/v1/bind/check-phone

```python
@bind_app.get("/api/v1/bind/check-phone")
async def check_phone(phone: str = ""):
    """验证手机号是否已绑定。"""
    if not phone:
        raise HTTPException(400, "手机号必填")
    if pg_store and pg_store.enabled:
        existing = await pg_store.find_binding_by_phone(phone)
        if existing:
            return {"exists": True, "bound_at": existing["bound_at"]}
    return {"exists": False}
```

### 3. service.py — register_binding 加二次检查

在分配槽位前再次检查（防并发）：

```python
# 手机号去重检查（全局）
if pg_store and pg_store.enabled:
    existing = await pg_store.find_binding_by_phone(phone)
    if existing:
        raise HTTPException(409, f"该手机号已在 {existing['machine_ip']} 上绑定了，不能重复绑定")
```

### 4. static/index.html — 前端两步页面

**页面结构：**

```
Step 0: 手机号输入页（默认显示）
  - 输入框：手机号
  - 按钮：验证
  - 错误提示：已绑定时的提示信息

Step 1: 信息填写页（手机号验证通过后显示）
  - 显示已验证的手机号
  - 用户名输入框
  - 龙虾名输入框
  - 提交按钮："开始绑定"

Step 2: 扫码页（同现有）
```

**改动逻辑：**

```javascript
// 新增：手机号验证
async function verifyPhone() {
  const phone = document.getElementById('inputPhone').value.trim();
  if (!phone) { alert('请填写手机号'); return; }
  
  const resp = await fetch(`/api/v1/bind/check-phone?phone=${encodeURIComponent(phone)}`);
  const data = await resp.json();
  
  if (data.exists) {
    showPhoneError('该手机号已绑定，不能重复绑定');
    return;
  }
  
  // 手机号可用 → 显示下一步
  document.getElementById('pagePhone').classList.remove('active');
  document.getElementById('pageForm').classList.add('active');
  document.getElementById('verifiedPhone').textContent = phone;
}

// 修改：submitForm 保留 phone 字段
async function submitForm() {
  const phone = document.getElementById('verifiedPhone').textContent;
  const user_name = document.getElementById('inputUser').value.trim();
  const lobster_name = document.getElementById('inputLobster').value.trim();
  
  const resp = await fetch('/api/v1/bind/register', {
    method: 'POST',
    body: JSON.stringify({ phone, user_name, lobster_name }),
  });
  // ... 同现有的提交后逻辑
}
```

### 5. hot_pool.py — _on_confirmed 二次去重（PG）

扫码确认后再次检查手机号（兜底），找到则复用/重启已有容器：

```python
async def _on_confirmed(self, result: dict):
    profile = result["profile"]
    slot = self.slots.get(profile)
    phone = (slot.user_info or {}).get("phone", "")
    
    # 手机号去重（全局）
    if phone and pg_store.enabled:
        existing = await pg_store.find_binding_by_phone(phone)
        if existing:
            logger.info("手机号 %s 已绑定（%s），复用容器", phone, existing["profile_name"])
            # 更新凭证 + 重启容器（不创建新的）
            ...
            return
    
    # 新用户：正常创建流程
    ...
```

## 实现步骤

| Step | 文件 | 改动 |
|------|------|------|
| 1 | `pg_store.py` | 加 `find_binding_by_phone(phone)` 全局查询 |
| 2 | `service.py` | 加 `GET /api/v1/bind/check-phone` 接口 |
| 3 | `service.py` | `register_binding` 加手机号二次检查 |
| 4 | `static/index.html` | 拆分为两步页面（手机号验证 → 信息填写 → 扫码） |
| 5 | `hot_pool.py` | `_on_confirmed` 改去重为查 PG（全局手机号） |

## 验证方式

1. 打开绑定页，输入已绑定的手机号 → 提示"已绑定"，无法进入下一步
2. 输入新手机号 → 进入填写页 → 提交 → 显示二维码
3. 同一个手机号在另一台机器上尝试 → 同样提示"已绑定"
4. 扫码确认后 → PG 有绑定记录，手机号唯一
