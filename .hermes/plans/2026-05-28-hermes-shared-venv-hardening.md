---
审核状态: 待审核
审核人: 
审核时间: 
审核意见: 
---

# 共享 Venv + 权限加固 — 项目代码化

## 目标

将之前手动在 dosh 服务器上执行的以下操作，写入项目代码，保证新部署时自动生效：

1. Hermes 共享 venv（`/opt/hermes/venv/`，非 editable 安装）
2. Service template 指向共享 venv
3. 权限加固（`.hermes/` 700, `hermes-agent/` 770, `o-w`）

## 变更清单

- **修改**: `systemd/hermes-gateway@.service`
  - `ExecStart` 从 `%h/.hermes/hermes-agent/venv/bin/python` → `/opt/hermes/venv/bin/python`
  - 移除 `HERMES_HOME=.../profiles/%i`（wx 用户用 700 隔离，无需 profile 切换）
  - PATH 从 `%h/.hermes/hermes-agent/venv/bin` → `/opt/hermes/venv/bin`

- **修改**: `scripts/setup.sh`
  - Step 1 后新增 Step 1.5：共享 venv 安装
    - 创建 `/opt/hermes/` 目录
    - 复制 hermes-agent 源码到 `/opt/hermes/hermes-agent/`
    - `uv pip install` 非 editable 安装到 `/opt/hermes/venv/`
    - 移除旧 editable pth 文件
  - Step 6 后新增权限加固步骤
    - `chmod -R o-w /opt/hermes/`
    - `chmod 770 /opt/hermes/hermes-agent/`

- **不修改**: `pool_manager/*.py`（已用 `/opt/hermes/venv/bin/python` 路径，无误）

## 实现步骤

1. 修改 `systemd/hermes-gateway@.service`
2. 修改 `scripts/setup.sh`
3. 本地语法检查（`bash -n setup.sh`）
4. 创建 PR → 合并

## 风险与注意事项

- `pip install` 在无网络环境可能失败 → 但 dosh 服务器有外网，且这次是本地目录 install（`pip install /opt/hermes/hermes-agent/`），不依赖网络
- `scripts/setup.sh` 中的 `$HOME` 是 dosh 用户，hermes source 在 `$HOME/.hermes/hermes-agent/`
- 如果 hermes-agent 已存在于 `/opt/hermes/hermes-agent/`，`cp` 会覆盖，不影响

## 验证方式

- `bash -n setup.sh` 语法检查通过
- 检查 service 模板中的 ExecStart 路径是否正确
- 在 dosh 服务器上重新跑 `setup.sh` 确认不报错
