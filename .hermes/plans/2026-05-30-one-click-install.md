# 一键部署增强计划

## 目标
单条命令 `curl -fsSL https://do.hermes.icu/install | bash` 完成裸机部署。

## 变更清单

### 文件 1: `scripts/setup.sh`（修改，+30行）

**① Docker 自动安装**
```bash
# 原来：检查不到 Docker 直接 exit 1
# 改为：自动安装
if ! command -v docker &>/dev/null; then
  echo "  Docker 未安装，自动安装..."
  curl -fsSL https://get.docker.com | bash
  sudo usermod -aG docker "$(whoami)"
  # 需要重新登录才能生效，但可以继续后面的步骤
fi
```

**② Docker Compose 插件自动安装**
```bash
if ! docker compose version &>/dev/null; then
  echo "  Docker Compose 未安装，自动安装..."
  sudo apt-get install -y docker-compose-plugin
fi
```

**③ API Key 交互式输入（新增 `--prompt-keys` 参数）**
```bash
# 在步骤 5（启动容器）之前新增
if [ "$PROMPT_KEYS" = true ] && [ ! -f "$DATA_ROOT/pool-manager/auth.json" ]; then
  echo "  ⚠️  未检测到 auth.json，请至少输入一个 DeepSeek API Key："
  read -p "  DeepSeek API Key (或留空跳过): " FIRST_KEY
  if [ -n "$FIRST_KEY" ]; then
    mkdir -p "$DATA_ROOT/pool-manager"
    # 使用 proxy API 添加 key
    echo '{"deepseek":["'"$FIRST_KEY"'"]}' > "$DATA_ROOT/pool-manager/auth.json"
    echo "  auth.json 已创建"
  fi
fi
```

### 文件 2: `scripts/install.sh`（新增，~40行）

```bash
#!/bin/bash
# WeChat Gateway Pool Manager 一键安装脚本
# 用法: curl -fsSL https://raw.githubusercontent.com/.../install.sh | bash
#       curl -fsSL https://raw.githubusercontent.com/.../install.sh | bash -s -- --total 500 --hot-pool 5

set -euo pipefail

# 1. 安装 Docker
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | bash
  sudo usermod -aG docker "$(whoami)"
fi

# 2. 下载项目代码
PROJECT_DIR="$HOME/wechat-pool-manager"
if [ ! -d "$PROJECT_DIR" ]; then
  echo "下载项目代码..."
  cd /tmp
  curl -L https://github.com/jiangboli/wechat-pool-manager/archive/refs/heads/main.tar.gz | tar xz
  mv wechat-pool-manager-main "$PROJECT_DIR"
fi

# 3. 执行 setup（带 --prompt-keys）
cd "$PROJECT_DIR"
exec bash scripts/setup.sh --prompt-keys "$@"
```

### 文件 3: `pool_manager/proxy.py`（可选，加强密钥持久化）
当前已有 `auth.json` 持久化逻辑，不需要改。

## 实现步骤

1. 修改 `scripts/setup.sh` — Docker 自安装 + key 输入
2. 创建 `scripts/install.sh` — 一键入口
3. 自测试：本地跑 `bash scripts/install.sh --help` 验证语法
4. PR → 合并 → 部署到 dosh（或用户在目标机器直接跑）

## 验证方式
- 在裸机上: `curl -fsSL https://raw.githubusercontent.com/jiangboli/wechat-pool-manager/main/scripts/install.sh | bash`
- 应自动装 Docker、下载代码、提示输入 Key、启动 3 个容器
- 容器 `pool-bind/admin/proxy` 全部 Up

## 风险
| 风险 | 缓解 |
|------|------|
| `curl | bash` 安全顾虑 | 从 GitHub 官方源拉取，代码公开可审查 |
| Docker 安装后需重启才能生效 | 后续的 docker compose 命令会被 sudo 执行通过 |
| git clone 在 China 慢 | 改用 tarball 下载（curl），比 git clone 快很多 |
| read 交互式在 pipe 模式下不可用 | `curl | bash` 中 read 失效。方案：install.sh 支持环境变量 `DEEPSEEK_KEY=xxx | bash` |
