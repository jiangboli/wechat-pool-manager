---
name: container-limits-plan
审核状态: 待审核
审核人:
审核时间:
审核意见:
---

# 容器存储 + 网络带宽控制方案

## 目标
限制每个微信 bot 容器的磁盘用量（可写层 + 数据目录）和网络带宽。

---

## 磁盘限制方案

### 现状
- 文件系统：`/home` 为 **EXT4**（非 XFS），不支持 project quota
- 两部分数据：
  | 类型 | 路径 | 当前最大 |
  |------|------|---------|
  | ① 可写层 | Docker overlay 层 | ~15 GB（004 历史值）|
  | ② Profile 数据 | `/home/dosh/data/{tail}/weixin-xxx/` | ~685 MB（011）|
- 可写层膨胀根因：用户 `pip install` 安装包，写入 overlay 层

### 方案：四层防御

```
Layer 4: Scheduler 自动重建 ← 兜底，可写层超限则 recreate
Layer 3: 数据目录监控告警 ← 数据目录超限报警
Layer 2: EXT4 user quota   ← 总盘保护（dosh 用户上限）
Layer 1: 源头优化          ← Dockerfile 只装必要依赖
```

---

#### Layer 2 — EXT4 User Quota（总盘保护）

```bash
# 1. /etc/fstab 添加 usrquota
# /dev/nvme0n1p3  /home  ext4  defaults,usrquota  0  2

# 2. remount + quota 初始化
mount -o remount,usrquota /home
quotacheck -ugm /home
quotaon /home

# 3. 限制 dosh 用户：软限 100GB，硬限 150GB
edquota -u dosh
```

#### Layer 4 — Scheduler 自动重建（核心保障）

在 `docker_scheduler.py` 的健康检查循环中增加：

```
_health_check_for_container()
  ↓
docker inspect --format '{{json .SizeRw}}' container
  ↓
SizeRw > 1GB 且连续 3 次超过阈值?
  ├─ Yes → docker kill + rm + spawn_container()
  └─ No  → 正常跳过
```

- 安全：profile 数据在 volume mount 中，recreate 后恢复
- 频率：当前健康检查间隔 900s
- 正常容器可写层 10-35MB，1GB 阈值足够宽裕

---

## 网络带宽控制方案

### 现状
- 所有容器通过 `hermes-pool-net` bridge 网络
- 微信流量轻量（< 1Mbps/容器），LLM API 走 pool-proxy 集中
- 主要带宽风险：Docker 镜像拉取占满出口

### 方案：tc 物理出口限速

```bash
# 限制 docker bridge 总出口 500Mbps
tc qdisc add dev docker0 root handle 1: htb default 30
tc class add dev docker0 parent 1: classid 1:1 htb rate 500mbps ceil 500mbps
tc class add dev docker0 parent 1:1 classid 1:10 htb rate 400mbps ceil 500mbps
tc class add dev docker0 parent 1:1 classid 1:20 htb rate 100mbps ceil 200mbps
```

持久化：写入 `/etc/networkd-dispatcher/routable.d/99-tc-limits` 或 `/etc/rc.local`

---

## 实施分 4 个 Phase

| Phase | 内容 | 涉及文件 | 风险 |
|-------|------|---------|------|
| **1** | EXT4 user quota（fstab + quotaon + edquota） | `/etc/fstab` | 低 |
| **2** | Scheduler 自动重建（SizeRw 检查 + recreate） | `docker_scheduler.py` | 中 |
| **3** | 数据目录监控告警 + 管理 API | `docker_scheduler.py`, `service.py` | 低 |
| **4** | tc 网络限速（docker0 出口） | `/etc/network/` | 低 |

---

## 风险与注意

1. **容器重建：** volume mount 中 `state.db` 等会话数据不受影响
2. **阈值选择：** 1GB 可写层阈值，当前正常容器 10-35MB，留裕量够
3. **网络限速：** 微信对消息延迟敏感，总出口设 500Mbps 足够
4. **setup.sh 集成：** Phase 1 的 fstab 修改要加到 setup.sh

---

## 验证方式

| Phase | 验证 |
|-------|------|
| 1 | `quota -u dosh` 显示正确限制 |
| 2 | 给容器写 1.1G 测试文件 → 3 轮健康检查后自动重建 |
| 3 | 数据目录写 600MB → 告警日志 |
| 4 | `tc -s qdisc show dev docker0` 确认速率 |
