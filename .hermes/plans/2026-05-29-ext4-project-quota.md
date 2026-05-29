---
name: ext4-project-quota-plan
审核状态: 
审核人: 
审核时间: 
审核意见: 
---

# ext4 project quota 限制每个用户 10G — 开发计划

## 目标
每个 WeChat 用户的数据目录自动被 ext4 project quota 限制在 10GB。

## 架构

```
宿主机 (dosh)
  ├── tune2fs -O project                    # 启用 ext4 project 特性
  ├── fstab: prjquota                       # 挂载配额
  ├── /etc/projid: 1~300 → "profile-001"    # project 名称 → ID 映射
  ├── /etc/projects: 路径 → project ID      # 路径 → ID 映射
  └── setquota -P <ID> 0 10G 0 0 /home      # 预置所有 300 个 slot 的配额

Docker pool-manager (需要 --cap-add SYS_ADMIN)
  └── create_container() → ensure_data_dir()
       └── chattr -p <project_id> /home/data/{尾数}/{profile}/
            ↑ 设置目录的 project ID，自动计入配额
```

## 实现思路

系统配置一次搞定，Docker 容器只需 `chattr -p <id>`（需要 SYS_ADMIN cap）：

1. **setup-quota.sh** — 一次性系统配置
   - 启用 ext4 project 特性
   - 修改 fstab + remount
   - 为全部 N 个 profile 预生成 project ID → 设置 quota

2. **docker_scheduler.py** — 代码改动
   - `__init__()` 读取 `storage_limit` 配置（默认 10G）
   - `ensure_data_dir()` 创建目录后执行 `chattr -p <project_id>` 
   - pool-manager 容器需要 `--cap-add SYS_ADMIN`

3. **setup.sh** — 部署脚本
   - 新增 `--storage-limit` 参数
   - 调用 setup-quota.sh

4. **已有容器迁移** — `scripts/migrate-quota.sh`
   - 遍历已有 profile 目录，设置 project ID

## 变更清单
- 新建: `scripts/setup-quota.sh` — 系统级 quota 初始化
- 新建: `scripts/migrate-quota.sh` — 已有目录迁移
- 修改: `pool_manager/docker_scheduler.py`
  - `__init__()` 加 `self.storage_limit` 参数
  - `ensure_data_dir()` 设 project ID
- 修改: `pool_manager/docker_scheduler.py` 的容器启动参数 — 加 `--cap-add SYS_ADMIN`
- 修改: `scripts/setup.sh` — 集成 quota 初始化

## 风险
- ⚠️ `tune2fs -O project` + `prjquota` remount 需要重启或卸载重挂
- ⚠️ pool-manager 重启不影响已有容器（已有目录没 project ID 的不受配额限制）
- ✅ `chattr` 通过 bind mount 在容器内执行生效（ext4 属性透传）
- ✅ quota 预置为所有 profile，`chattr -p` 只是把目录绑到已配置的 quota

请审核是否可以按此计划执行？