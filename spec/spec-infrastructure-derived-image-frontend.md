---
title: 派生镜像封装前端改动（后端仍 patch 挂载）+ 现架构回滚基线
version: 1.0
date_created: 2026-06-17
last_updated: 2026-06-17
owner: shiyaqi (openhands 多用户)
tags: [infrastructure, docker, frontend, multi-user, openhands, rollback]
---

# Introduction

把当前**运行时挂载**的两个前端文件（`login.html`、`index.html`）改为**烤进一个派生镜像**（`FROM ghcr.io/openhands/openhands:latest` + `COPY`），用该派生镜像替换原镜像启动 app-server。**后端 4 个 Python patch 维持运行时挂载不变**。本规格同时**完整记录当前架构作为回滚基线**——若派生镜像方案失败，可一键回到现状。

本规格基于对运行中容器（`openhands-app`）的实测核实（镜像 ID、env、端口、前端路径均已确认，见 §7）。

## 1. Purpose & Scope

**目的**：减少 app-server 的前端运行时挂载，使前端改动自包含于一个可版本化的镜像；同时保留后端 patch 的灵活性（升级官方版只需重对 py 文件）。

**范围内**：
- 编写 `Dockerfile`：`FROM ghcr.io/openhands/openhands:latest`，`COPY` 两个前端文件进 `/app/frontend/build/`。
- 构建本地派生镜像（如 `openhands-multiuser:1.0`）。
- 用派生镜像替换原镜像重启 app-server；**去掉两个前端文件的 `-v` 挂载**，**保留 4 个后端 py patch 挂载**。
- 完整记录当前架构作为回滚基线（§4.4、§9.3）。

**范围外**：
- 后端 patch 的任何改动（维持运行时挂载，不进镜像）。
- 前端**源码**级修改 / `pnpm build` 重新编译（本方案仍用预编译 bundle + 覆盖 `index.html`，与现状一致，仅改"覆盖手段"从挂载变 COPY）。
- gateway.py、sandbox 镜像、env 变量、`settings.json` —— 均不变。

**目标读者**：执行该基础设施变更的工程师 / AI 编码助手。

## 2. Definitions

| 术语 | 定义 |
|---|---|
| **原镜像** | `ghcr.io/openhands/openhands:latest`（实测 ID `sha256:6ae36746bde4...`，2026-06-10 构建）。 |
| **派生镜像** | 本方案新建的本地镜像 `openhands-multiuser:1.0`，基于原镜像 + COPY 前端文件。 |
| **前端文件** | `login.html`（登录页）、`index.html`（含 X-User-Id 注入脚本），现位于宿主 `/home/mocca/openhands/frontend-patch/`。 |
| **后端 patch** | 4 个 Python 文件，位于宿主 `/home/mocca/openhands/patches/`，运行时只读挂载覆盖容器内文件。 |
| **运行时挂载** | docker `-v 宿主路径:容器路径:ro`，容器启动时覆盖镜像内文件。 |
| **回滚基线** | 当前正在运行的架构（原镜像 + 6 个挂载），记录于 §4.4，失败时据此还原。 |

## 3. Requirements, Constraints & Guidelines

### 功能需求
- **REQ-001**：提供 `Dockerfile`，基础镜像为 `ghcr.io/openhands/openhands:latest`。
- **REQ-002**：`Dockerfile` 将 `login.html` 与 `index.html` `COPY` 到镜像内 `/app/frontend/build/`（实测确认的前端目录）。
- **REQ-003**：构建产物为带版本标签的本地镜像 `openhands-multiuser:1.0`。
- **REQ-004**：用派生镜像启动 app-server，启动命令**去掉**两个前端文件的 `-v` 挂载。
- **REQ-005**：启动命令**保留** 4 个后端 py patch 的 `-v ...:ro` 挂载（行为与现状一致）。
- **REQ-006**：启动命令保留所有现有 env、端口 `-p 3000:3000`、卷挂载（docker.sock / workspace / state）。
- **REQ-007**：派生镜像内 `COPY` 进去的文件与当前挂载的文件**内容一致**（同一份 `frontend-patch/` 来源）。

### 约束
- **CON-001**：后端 patch **不得**进镜像——保持运行时挂载，便于升级官方版时单独重对。
- **CON-002**：不修改前端 bundle（`assets/*.js`），仅 COPY 覆盖 `index.html` + 新增 `login.html`，与现状覆盖逻辑一致。
- **CON-003**：派生镜像构建后，原镜像 `ghcr.io/openhands/openhands:latest` **必须保留在本地**（`docker images` 可见），作为回滚基础。
- **CON-004**：`frontend-patch/` 源文件**必须保留**——既是镜像构建来源，也是回滚时重新挂载的来源。
- **CON-005**：切换期间 :3000 短暂不可用（停旧容器→起新容器，秒级），可接受。

### 指南
- **GUD-001**：镜像标签带语义版本（`1.0`），前端再改时递增（`1.1`…），便于追溯与回滚到上一镜像。
- **GUD-002**：`Dockerfile` 与构建脚本放 `/home/mocca/openhands/docker/`，与挂载文件分离。
- **GUD-003**：构建用 `docker build`，无需 BuildKit 特性；保持可在 3.5G 机器上完成（仅 COPY，几乎不耗资源）。

### 模式
- **PAT-001**：派生镜像模式（`FROM 官方镜像 + COPY`）——保留"不维护 fork、不搭前端构建环境"的优点，同时前端自包含。

## 4. Interfaces & Data Contracts

### 4.1 Dockerfile 契约

```dockerfile
# /home/mocca/openhands/docker/Dockerfile
FROM ghcr.io/openhands/openhands:latest
# 覆盖 index.html（注入 X-User-Id 脚本）+ 新增 login.html（登录页）
COPY login.html /app/frontend/build/login.html
COPY index.html /app/frontend/build/index.html
```

### 4.2 构建契约

| 项 | 值 |
|---|---|
| 构建上下文 | `/home/mocca/openhands/docker/`（含 Dockerfile + 两个 html 副本，或用 `-f` 指向 frontend-patch） |
| 镜像名:标签 | `openhands-multiuser:1.0` |
| 构建命令 | `docker build -t openhands-multiuser:1.0 -f docker/Dockerfile .`（上下文需含两个 html） |

### 4.3 启动命令契约（变更后）

相对当前启动命令（§4.4）**两处变化**：
1. 镜像 `ghcr.io/openhands/openhands:latest` → `openhands-multiuser:1.0`
2. **删除**两行前端挂载：
   ```
   -v .../frontend-patch/login.html:/app/frontend/build/login.html:ro   ← 删
   -v .../frontend-patch/index.html:/app/frontend/build/index.html:ro   ← 删
   ```
其余全部保留（4 个 py patch 挂载 + env + 端口 + 卷）。

### 4.4 回滚基线（当前运行架构，实测 2026-06-17）

**镜像**：`ghcr.io/openhands/openhands:latest`（ID `sha256:6ae36746bde4986483a0e1c543e7880abe01fb1b4c610e771da83b5f2106436a`）

**完整启动命令（当前正在运行的配置，回滚即用此）**：
```bash
docker run -d --restart unless-stopped \
  -e SANDBOX_USER_ID=1001 \
  -e INIT_GIT_IN_EMPTY_WORKSPACE=1 \
  -e SANDBOX_LOCAL_RUNTIME_URL=http://host.docker.internal \
  -e OH_PERMITTED_CORS_ORIGINS_0='http://43.162.100.45:3000' \
  -e OH_MAX_CONV_PER_SANDBOX=20 \
  -e OH_MAX_NUM_SANDBOXES=15 \
  -e WORKSPACE_MOUNT_PATH=/home/mocca/openhands/workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /home/mocca/openhands/workspace:/opt/workspace_base \
  -v /home/mocca/openhands/.openhands-state:/.openhands \
  -v /home/mocca/openhands/patches/docker_sandbox_service.py:/app/openhands/app_server/sandbox/docker_sandbox_service.py:ro \
  -v /home/mocca/openhands/patches/live_status_app_conversation_service.py:/app/openhands/app_server/app_conversation/live_status_app_conversation_service.py:ro \
  -v /home/mocca/openhands/patches/default_user_auth.py:/app/openhands/app_server/user_auth/default_user_auth.py:ro \
  -v /home/mocca/openhands/patches/sql_app_conversation_info_service.py:/app/openhands/app_server/app_conversation/sql_app_conversation_info_service.py:ro \
  -v /home/mocca/openhands/frontend-patch/login.html:/app/frontend/build/login.html:ro \
  -v /home/mocca/openhands/frontend-patch/index.html:/app/frontend/build/index.html:ro \
  -p 3000:3000 --add-host host.docker.internal:host-gateway \
  --name openhands-app ghcr.io/openhands/openhands:latest
```

**配套（均不在 app-server 镜像内，变更后仍需独立存在）**：
- `gateway.py` 宿主进程（:8000，按 session_api_key 路由）
- `.openhands-state/settings.json` 含 `sandbox_grouping_strategy=GROUP_BY_NEWEST`
- env：`OH_MAX_NUM_SANDBOXES=15`、`OH_MAX_CONV_PER_SANDBOX=20`
- sandbox 镜像：`ghcr.io/openhands/agent-server:1.27.1-python`

## 5. Acceptance Criteria

- **AC-001**：Given `Dockerfile` 与两个 html，When `docker build -t openhands-multiuser:1.0`，Then 构建成功且 `docker images` 可见该镜像。
- **AC-002**：Given 派生镜像，When 启动 app-server（无前端挂载、保留 py patch 挂载），Then 容器 Up 且日志 "Application startup complete"。
- **AC-003**：Given 派生镜像运行中，When `docker exec openhands-app cat /app/frontend/build/login.html`，Then 内容与 `frontend-patch/login.html` 一致（COPY 生效）。
- **AC-004**：Given 派生镜像运行中，When `curl http://localhost:3000/login.html`，Then 返回登录页（HTTP 200，含 "OpenHands 登录"）。
- **AC-005**：Given 派生镜像运行中，When `curl http://localhost:3000/`，Then `index.html` 含 `X-User-Id` 注入脚本。
- **AC-006**：The system shall 保持多用户功能不变——alice/bob 登录、会话隔离、每用户独占 sandbox，行为与挂载方案完全一致。
- **AC-007**：Given 后端 4 个 py patch 仍以挂载方式存在，When `docker inspect` 查挂载，Then 4 个 py patch 挂载在、2 个前端挂载不在。
- **AC-008**：The system shall 在派生镜像方案失败时，按 §4.4 / §9.3 回滚基线一键还原到挂载方案。

## 6. Test Automation Strategy

- **Test Levels**：构建验证 + 部署冒烟 + 端到端手验。
- **Frameworks**：无（基础设施变更）；用 `docker build`、`docker inspect`、`curl`、浏览器。
- **Test Data Management**：测试用户 alice/bob；测试后清理会话 + sandbox（见 ../docs/current/architecture.md §9.7）。
- **CI/CD Integration**：不适用（手工构建）。
- **Coverage Requirements**：覆盖 §5 全部 AC。
- **Performance Testing**：不适用（COPY 构建，运行时性能与原镜像一致）。

## 7. Rationale & Context

### 为什么"派生镜像 + 后端仍挂载"是最优折中
- 前端进镜像 → 自包含、可版本化、`docker run` 少两个挂载、便于分发。
- 后端仍挂载 → 升级官方 `latest` 时只需重对 4 个 py 文件，不必每次重 build；patch 调试改完重启即可，无构建回合。
- 不碰前端 bundle、不搭 pnpm 构建环境、不维护 fork —— 维护成本最低。

### 为什么功能零差异
当前挂载的文件 = 要 COPY 进镜像的文件（同一份 `frontend-patch/` 来源，REQ-007）。"挂载覆盖"与"COPY 进镜像"对运行时是等价的文件替换，app-server 看到的文件字节一致 → 行为一致。

### 为什么必须保留回滚基线
- 原镜像保留（CON-003）+ `frontend-patch/` 保留（CON-004）→ 回滚只是换回镜像名 + 加回两行挂载。
- 后端 patch 与本变更正交，回滚不涉及后端。

### 实测核实结论（§具体值）
- 镜像内前端路径确认：`/app/frontend/build/{index,login}.html` 存在。
- 当前完整 env/端口已抓取（§4.4），回滚命令精确可用。
- 原镜像 ID `sha256:6ae36746bde4...`，回滚时确认未被覆盖。

## 8. Dependencies & External Integrations

### External Systems
- **EXT-001**：`ghcr.io/openhands/openhands:latest` —— 派生镜像的基础镜像（需本地已 pull）。

### Infrastructure Dependencies
- **INF-001**：Docker（支持 `docker build`）。
- **INF-002**：gateway.py 宿主进程（:8000）—— 不变，仍需运行。
- **INF-003**：`.openhands-state` 持久卷（含 settings.json 的 GROUP_BY_NEWEST）—— 不变。

### Data Dependencies
- **DAT-001**：`frontend-patch/login.html` + `index.html` —— 构建输入，必须保留。
- **DAT-002**：`patches/*.py`（4 个）—— 运行时挂载输入，不变。

### Technology Platform Dependencies
- **PLT-001**：磁盘空间 —— 派生镜像与原镜像共享基础层，仅增量极小（两个 html，KB 级）。

### Compliance Dependencies
- 无。

## 9. Examples & Edge Cases

### 9.1 构建 + 切换（推荐步骤）

```bash
# 0) 确保原镜像在本地（回滚基础，CON-003）
docker images | grep openhands/openhands   # 应见 latest

# 1) 准备构建上下文
mkdir -p /home/mocca/openhands/docker
cp /home/mocca/openhands/frontend-patch/login.html /home/mocca/openhands/docker/
cp /home/mocca/openhands/frontend-patch/index.html /home/mocca/openhands/docker/
# 写入 §4.1 的 Dockerfile 到 /home/mocca/openhands/docker/Dockerfile

# 2) 构建派生镜像
cd /home/mocca/openhands/docker
docker build -t openhands-multiuser:1.0 .

# 3) 停旧容器（数据在卷里，安全）
docker rm -f openhands-app

# 4) 用派生镜像起（无前端挂载，保留 py patch 挂载）
docker run -d --restart unless-stopped \
  -e SANDBOX_USER_ID=1001 -e INIT_GIT_IN_EMPTY_WORKSPACE=1 \
  -e SANDBOX_LOCAL_RUNTIME_URL=http://host.docker.internal \
  -e OH_PERMITTED_CORS_ORIGINS_0='http://43.162.100.45:3000' \
  -e OH_MAX_CONV_PER_SANDBOX=20 -e OH_MAX_NUM_SANDBOXES=15 \
  -e WORKSPACE_MOUNT_PATH=/home/mocca/openhands/workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /home/mocca/openhands/workspace:/opt/workspace_base \
  -v /home/mocca/openhands/.openhands-state:/.openhands \
  -v /home/mocca/openhands/patches/docker_sandbox_service.py:/app/openhands/app_server/sandbox/docker_sandbox_service.py:ro \
  -v /home/mocca/openhands/patches/live_status_app_conversation_service.py:/app/openhands/app_server/app_conversation/live_status_app_conversation_service.py:ro \
  -v /home/mocca/openhands/patches/default_user_auth.py:/app/openhands/app_server/user_auth/default_user_auth.py:ro \
  -v /home/mocca/openhands/patches/sql_app_conversation_info_service.py:/app/openhands/app_server/app_conversation/sql_app_conversation_info_service.py:ro \
  -p 3000:3000 --add-host host.docker.internal:host-gateway \
  --name openhands-app openhands-multiuser:1.0
```

### 9.2 回滚（派生镜像失败时，恢复挂载方案）

```bash
docker rm -f openhands-app
# 直接执行 §4.4 的完整启动命令（原镜像 + 6 个挂载）
# 前提：frontend-patch/ 与 patches/ 均未删（CON-003/004）
```

### 9.3 边界情况

| 情况 | 处理 |
|---|---|
| 前端再改一次 | 更新 `frontend-patch/*.html` → 重新 `cp` 到 docker/ → `docker build -t openhands-multiuser:1.1` → 换标签重启。旧镜像 1.0 留作回滚 |
| 升级官方 latest | `docker pull` 新 latest → 重新 build 派生镜像（前端不变）→ 后端 4 个 py patch 需对照新版重 diff（与现状同） |
| COPY 与挂载内容不一致 | 违反 REQ-007；以 `frontend-patch/` 为唯一真源，构建前重新 cp |
| 误删原镜像 | `docker pull ghcr.io/openhands/openhands:latest` 重新拉（注意官方 latest 可能已更新，行为或有差异）|
| 构建上下文无 html | `docker build` 报 COPY 找不到文件；确保 cp 到 docker/ 或调整 `-f` 与上下文 |
| docker.sock / 卷未挂 | app-server 无法拉 sandbox；保留 §4.4 所有挂载 |

## 10. Validation Criteria

1. `docker build` 成功，`docker images` 见 `openhands-multiuser:1.0`。
2. `docker exec openhands-app cat /app/frontend/build/login.html` 与 `frontend-patch/login.html` diff 为空。
3. `curl http://localhost:3000/login.html` 返回登录页（200）。
4. `curl http://localhost:3000/` 的 index.html 含 X-User-Id 注入脚本。
5. `docker inspect openhands-app` 挂载中：4 个 py patch 在、2 个前端文件不在（AC-007）。
6. 浏览器 alice/bob 登录、隔离、各自 sandbox —— 与挂载方案行为一致。
7. 原镜像 `ghcr.io/openhands/openhands:latest` 仍在本地（回滚就绪）。
8. 执行 §9.2 回滚命令可恢复到挂载方案并正常工作。

## 11. Related Specifications / Further Reading

- `/home/mocca/openhands/spec/spec-design-frontend-login-userid.md` —— 前端登录 + X-User-Id 注入的设计（本规格改变其"部署手段"，不改其逻辑）。
- `/home/mocca/openhands/spec/plan-frontend-login-userid.md` —— 前端登录实施计划。
- `/home/mocca/openhands/docs/current/architecture.md` §9 / §9.10 —— 多用户方案全貌与当前架构。
- §4.4 本文 —— 当前架构回滚基线（实测快照）。
