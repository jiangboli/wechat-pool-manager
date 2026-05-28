# WeChat Gateway Pool Manager v3 (Docker)

为多个微信用户提供独立的 Hermes Gateway 实例的池管理器。

每个用户通过扫码绑定自己的微信 bot，获得一个专属的 Docker 容器。

## 架构（Docker 容器化）

```
WeChat iLink
      │
      ▼
pool-manager (Docker, :8765)
├── 热池: 保持 3-5 个 QR 扫码槽位
├── Docker 调度: 创建/启动/停止/重启容器
├── LLM Proxy: 负载均衡 + 熔断保护 + 多 Provider
├── 健康检查: 每 60s 检查所有容器
└── 前端页面: 扫码绑定

每个 bound profile
  → Docker 容器 (hermes-wx001 / hermes-wx002 / ...)
  → 独立 ~/.hermes/ (volume mount: /home/data/{尾数}/{profile}/.hermes/)
  → LLM 请求经过 pool manager proxy (:8765/v1)
  → API key 只在 pool manager 内存
```

## 快速开始

```bash
# 前提：服务器已安装 Docker
# 1. 按机器配置部署
bash scripts/setup.sh --total 50 --hot-pool 5

# 2. 添加 LLM API Key
curl -X POST http://<your-ip>:8765/api/v1/proxy/keys \
  -H 'Content-Type: application/json' \
  -d '{"provider":"deepseek","key":"sk-xxx-1","label":"主key-1"}'

# 3. 打开前端页面扫码绑定
open http://<your-ip>:8765
```

## 参数参考

| 参数 | 说明 | 默认 | 4GB 建议 | 8GB 建议 | 16GB 建议 |
|------|------|------|----------|----------|-----------|
| `--total` | profile 总数 | 100 | 30 | 60 | 100 |
| `--hot-pool` | 热池大小 | 5 | 3 | 4 | 5 |
| `--max-bound` | 最大运行容器 | 80 | 15 | 40 | 80 |

## 数据目录

```
/home/data/
├── 0/wx010/.hermes/     ← wx010 尾数 0
├── 1/wx001/.hermes/     ← wx001 尾数 1
├── 1/wx011/.hermes/     ← wx011 尾数 1
├── ...
└── 9/wx009/.hermes/     ← wx009 尾数 9
```

按 profile 编号末位 0-9 分散到 10 个目录，减少单目录文件数。

## LLM Proxy 管理

Pool Manager 内置 OpenAI 兼容的 LLM 代理，所有微信用户的 LLM 请求统一经过此代理：

```bash
# 添加 API Key
curl -X POST http://localhost:8765/api/v1/proxy/keys \
  -d '{"provider":"deepseek","key":"sk-xxx","label":"主key"}'

# 查看状态
curl http://localhost:8765/api/v1/proxy/status

# 删除 Key
curl -X DELETE http://localhost:8765/api/v1/proxy/keys/{key_id}
```

**特性：**
- 多 Provider 负载均衡（round-robin）
- 熔断保护（连续 5 次错误自动暂停使用）
- Fallback provider 链（主 provider 不可用时自动切换）
- 调用量统计

## 管理命令

```bash
# 查看 Pool Manager 日志
docker logs -f pool-manager

# 查看所有微信容器
docker ps --filter label=managed_by=pool-manager

# 查看某个用户容器日志
docker logs hermes-weixin-001

# 池统计
curl http://localhost:8765/api/v1/pool/stats

# 健康检查
curl http://localhost:8765/health
```

## 移植到新机器

```bash
# 1. 新机器安装 Docker
curl -fsSL https://get.docker.com | bash

# 2. 克隆项目
git clone https://github.com/jiangboli/wechat-pool-manager.git
cd wechat-pool-manager

# 3. 按机器配置部署
bash scripts/setup.sh --total 30 --hot-pool 3
```
