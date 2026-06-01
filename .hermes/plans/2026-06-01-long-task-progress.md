---
审核状态: 通过 ✅
审核人: jiangboli (用户)
审核时间: 2026-06-01
审核意见: "可以，按照开发流程走"
---

# 长任务业务进度通知

## 目标

微信 bot 容器在执行耗时任务（超过 30 秒）时，自动向用户发送**业务层面的进度更新**，每 60 秒一次。不发送任何工具调用级别的噪音消息。

## 变更清单

- 修改: `pool_manager/docker_scheduler.py` — `write_config()` 方法

## 实现步骤

1. config 模板中增加 `display` 段（关闭所有内置噪音）+ `messaging` toolset
2. AGENTS.md 模板改为精确的业务进度指令（不去匹配已有的，每次都写入最新版）
3. 去掉 `if not os.path.exists` 条件，AGENTS.md 每次都覆盖写入（让已有容器在重启时也获得新规则）
4. 编译/语法检查 → 单元测试 → 运行验证
5. PR → merge → 部署到 dosh 服务器

## 环境/依赖审计

- 仅修改 `docker_scheduler.py`，不涉及新依赖
- 依赖 Hermes Agent 的 `send_message` 工具和 `messaging` toolset，bot 容器中的 Hermes 版本已支持

## 部署影响

- 新绑定的容器：config + AGENTS.md 立即生效
- 已有容器：需要重启容器才能生效（config.yaml 中的 `.managed` 标记防止 Hermes 自动重写，但 pool-manager 的 `write_config()` 本身不会重写已有容器的 config，所以已有容器的 display 和 AGENTS.md 需要单独处理）

## 风险与注意事项

- **已有容器不会自动更新**：`write_config()` 只在绑定新容器时调用。已有 41 个容器需要用其它方式更新 AGENTS.md + config
- `messaging` toolset 必须有，否则 `send_message` 工具不可用
- AGENTS.md 是 soft instruction，agent 可能在复杂任务中"忘记"发进度，但比没有好

## 验证方式

1. 新绑定一个容器，检查 config.yaml 是否包含 display + messaging 配置
2. 检查 AGENTS.md 内容是否正确
3. 手动触发长任务，观察是否收到进度通知
