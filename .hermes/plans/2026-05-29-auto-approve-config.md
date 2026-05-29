---
name: auto-approve-config-plan
审核状态: 通过 ✅
审核人: jreye (用户)
审核时间: 2026-05-29
审核意见: "按计划执行！" 
---

# Docker 容器默认全部自动确认 — 开发计划

## 目标
在所有 hermes-bot Docker 容器的 config.yaml 中默认加入自动审批配置，让微信用户无需手动 approve 子任务和破坏性操作。

## 变更清单
- 修改: `pool_manager/docker_scheduler.py` — `write_config()` 方法中的 YAML 模板，新增 approval 配置

## 实现步骤
1. 修改 `docker_scheduler.py` 的 `write_config()`，在 YAML 模板中追加 `delegation.subagent_auto_approve: true` 和 `approvals.destructive_slash_confirm: false`
2. 本地编译检查（Python 语法检查）
3. 自测试（语法检查 + 单元测试）
4. 创建 PR、合并到 main
5. 部署到 dosh 服务器（118.122.92.55）
6. 重启 pool-manager 容器
7. 验证：检查新生成容器的 config.yaml 是否包含新增配置

## 风险与注意事项
- ⚠️ 已有 Docker 容器不会自动更新配置。新容器生效，旧容器需手动重建或滚动更新
- 当前在 hot_pool.py `_on_confirmed()` 中已有的 `self._scheduler.create_container(profile, ...)` 创建新容器时会自动调用 `write_config()`
- 配置变更不需要修改数据库或模型

## 验证方式
1. `git diff` 确认模板改动正确
2. 部署后 SSH 到 dosh 服务器，检查某已绑定 profile 的 config.yaml 是否包含 `subagent_auto_approve: true`
3. 创建新 profile 的 Docker 容器，检查配置是否生效