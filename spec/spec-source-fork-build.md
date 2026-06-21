---
title: 源码 fork 自构建镜像（路线 B）+ 兼容上游升级 + 前端深度定制
version: 1.0
date_created: 2026-06-21
owner: shiyaqi (openhands 多用户)
tags: [infrastructure, docker, fork, build, frontend, backend, multi-user, openhands, upgrade]
---

# Introduction

把当前**运行时挂载 6 个文件**（4 个后端 py patch + 2 个前端 html）的部署方式，升级为**从 OpenHands 源码 fork 构建一份自包含镜像**。改动直接并入 fork 源码、以可 rebase 的 commit 栈维护，借此**兼容上游持续升级**，并为后续**前端深度定制**打地基。运行时不再需要任何 patch 挂载。

本规格基于对运行中容器（`openhands-app`）与上游镜像/源码的实测核实（commit、Dockerfile、前端工具链、后端 delta 行数均已确认，见 §7）。

> **前置说明**：本方案是 [`spec-infrastructure-derived-image-frontend.md`](spec-infrastructure-derived-image-frontend.md)（派生镜像，仅前端进镜像、后端仍挂载）的**进一步演进**。那份派生镜像方案仍是有效的轻量回退选项；本方案走更重的源码 fork 路线，适合需要源码级（尤其前端）深度改动的目标。

## 1. Purpose & Scope

**核心目标（本期）**：**复现等价**——从 fork 源码构建出一份镜像，使其行为/内容与**当前正在运行的版本**（官方 `96eea1b` 基线 + 4 后端 patch + 2 前端文件）**完全一致**，作为后续定制的受信基线。本期不引入任何"对齐既有改动之外"的新行为；先证明"源码能复现现网"，再谈演进。

**后续阶段目标**（非本期，依赖核心目标先达成）：
- 用一份**自构建、版本化、自包含**的镜像替代"官方镜像 + 6 挂载"，运行时零 patch 挂载。
- 改动以源码形式存在于 fork，**可随上游升级 rebase**，而非每次升级重抠整文件 patch。
- 为前端深度定制（改源码而非覆盖编译产物）提供工程基线。

**范围内**：
- Fork OpenHands 源码、建立 `upstream`/`origin` 远程与长期定制分支 `multiuser-deploy`。
- 把现有 4 个后端 patch 的真实 delta 抽成补丁并入源码，组织为干净 commit。
- 前端从"覆盖编译产物"迁移到源码改动（分两步：先求平价，再做正统）。
- 在 **CI（GitHub Actions）或大内存机器**构建镜像并 push 到 registry；3.5G 机器只 pull 运行。
- 运行切换（去掉全部 6 挂载）与回滚。
- 常态化上游升级流程。

**范围外**：
- `gateway.py`、`agent-server`（sandbox）镜像、env、`settings.json`、`.openhands-state` —— 均不变。
- sandbox 镜像（`agent-server:1.27.1-python`）的 fork/定制（独立镜像，本方案不碰）。
- 在 3.5G 生产机上跑前端构建（明确禁止，见 CON-002）。

**目标读者**：执行该基础设施变更的工程师 / AI 编码助手。

## 2. Definitions

| 术语 | 定义 |
|---|---|
| **上游** | `github.com/OpenHands/OpenHands`，默认分支 `main`。 |
| **基线 commit** | `96eea1be4774f841c7685e95042c4b5a2afa43e1`——当前线上 `ghcr.io/openhands/openhands:latest` 的源码（OCI label `revision` 实测，见 §7）。 |
| **Fork** | `github.com/gongzishuye/OpenHands-custom`（本方案的源码仓，区别于运维补丁仓 `gongzishuye/openhands`）。 |
| **定制分支** | `multiuser-deploy`，基于基线 commit 拉出，承载所有自有改动（可 rebase 的 commit 栈）。 |
| **后端 delta** | 4 个 py 文件相对上游的真实改动，共 +100/-10 行（见 §7 表）。 |
| **自构建镜像** | 由 fork 源码构建的镜像，如 `ghcr.io/gongzishuye/openhands-custom:96eea1b-mu1`。 |
| **构建机** | CI（GitHub Actions）或 ≥8G 内存机器；**不是** 3.5G 生产机。 |
| **生产机** | 腾讯云 `43.162.100.45`（3.5G/2vCPU），只 pull + run 镜像。 |

## 3. Requirements, Constraints & Guidelines

### 功能需求
- **REQ-001**：建立 fork `gongzishuye/OpenHands-custom`，配置 `upstream`（OpenHands/OpenHands）与 `origin`（fork）两个远程。
- **REQ-002**：基于基线 commit `96eea1b` 拉出长期分支 `multiuser-deploy`；`main` 跟随上游、不直接提交。
- **REQ-003**：把 4 个后端 patch 的 delta 抽成 4 个 unified `.patch`，在 `multiuser-deploy` 上 `git apply` 并提交为逻辑 commit（后端隔离）。
- **REQ-004**：前端改动并入 `frontend/` 源码或经构建后覆盖产物，最终行为与现挂载方案一致（alice/bob 登录、X-User-Id 注入）。
- **REQ-005**：镜像在构建机（CI/大机器）构建，push 到 registry；产物带可追溯 tag（含上游基线标识）。
- **REQ-006**：生产机用自构建镜像启动 app-server，**去掉全部 6 个 patch 挂载**；其余 env/端口/卷/`gateway.py` 不变。
- **REQ-007**：提供上游升级的 rebase + 重构建 + 切换流程（§9.4）。

### 约束
- **CON-001**：改动以**源码 commit** 形式存在，不得回退到"运行时挂载整文件"——否则失去本方案意义。
- **CON-002**：**禁止在 3.5G 生产机构建前端**（`react-router build` 极可能 OOM）。构建只在 CI 或 ≥8G 机器进行。
- **CON-003**：原镜像 `ghcr.io/openhands/openhands:latest`（基线）与 `frontend-patch/`、`patches/` **必须保留**——回滚到挂载方案的基础（见 [`spec-infrastructure-derived-image-frontend.md`](spec-infrastructure-derived-image-frontend.md) §4.4）。
- **CON-004**：定制分支基于**固定 commit**而非滚动 `main` / `latest`——`:latest` 跟 main 滚动，"最新"≠"线上版"，升级须主动选基线。
- **CON-005**：fork 仓库名**不能**叫 `openhands`（GitHub 仓名大小写不敏感，与补丁仓 `gongzishuye/openhands` 冲突）——用 `OpenHands-custom`。
- **CON-006**：切换期间 :3000 短暂不可用（停旧→起新，秒级），可接受。

### 指南
- **GUD-001**：前端定制优先**新增文件 + 开关**，少改上游既有文件；必须改的集中并加标记注释——降低上游升级时的 rebase 冲突面。
- **GUD-002**：改动按职责拆成独立 commit（后端隔离 / 前端登录 / 前端深度定制…），便于 rebase 时逐个核对。
- **GUD-003**：镜像 tag 含上游基线标识（如 `96eea1b-mu1`），永远能回答"它基于哪个上游 commit"。
- **GUD-004**：每次构建后保留上一个可用镜像 tag，作为镜像级回滚点。

### 模式
- **PAT-001**：Fork + 长期定制分支 + 周期性 rebase——业界维护"带自有改动的上游软件"的标准做法。
- **PAT-002**：构建/运行分离——重构建放 CI/大机器，低配生产机只 pull 运行。

## 4. Interfaces & Data Contracts

### 4.1 远程与分支契约

```
origin    → git@github.com:gongzishuye/OpenHands-custom.git   # 你的 fork
upstream  → https://github.com/OpenHands/OpenHands.git        # 上游，只读同步

main              ← 镜像 upstream/main，自己不提交（干净基准）
multiuser-deploy  ← 基于基线 commit 96eea1b，承载所有自有改动
```

### 4.2 后端 delta 落点契约（源码路径 = 现挂载的容器路径）

| patch 文件（现 `patches/`） | fork 源码路径 | 改动量 |
|---|---|---|
| `default_user_auth.py` | `openhands/app_server/user_auth/default_user_auth.py` | +9/-4 |
| `docker_sandbox_service.py` | `openhands/app_server/sandbox/docker_sandbox_service.py` | +16/-4 |
| `sql_app_conversation_info_service.py` | `openhands/app_server/app_conversation/sql_app_conversation_info_service.py` | +16/-0 |
| `live_status_app_conversation_service.py` | `openhands/app_server/app_conversation/live_status_app_conversation_service.py` | +59/-2 |

> delta 由 "基线镜像内原文 `orig_*`" 与 "`patches/*`" diff 得出；因 orig 取自基线 commit 的镜像，补丁在 `96eea1b` 上必能干净 `git apply`。

### 4.3 前端落点契约

| 现状（挂载） | fork 源码侧做法 |
|---|---|
| 覆盖 `/app/frontend/build/index.html`（注入 X-User-Id） | 步骤①平价：构建后覆盖产物；步骤②正统：改 `frontend/` 源码（组件/路由层）后 `npm run build` 自然产出 |
| 新增 `/app/frontend/build/login.html`（登录页） | 同上 |

前端构建工具链（实测）：Node 22（`.nvmrc=22`，`engines.node>=22.12.0`），`npm@10.5.0`，build = `npm run make-i18n && react-router build`。

### 4.4 构建契约

| 项 | 值 |
|---|---|
| Dockerfile | 上游 `containers/app/Dockerfile`（三段式：node 构建前端 → poetry 装后端 → 组装）；改动已在源码，无需改 Dockerfile |
| 构建位置 | GitHub Actions 或 ≥8G 机器（CON-002） |
| 镜像名:tag | `ghcr.io/gongzishuye/openhands-custom:<上游基线>-mu<版本>`，如 `96eea1b-mu1` |
| 构建命令 | `docker build -f containers/app/Dockerfile -t <镜像:tag> --build-arg OPENHANDS_BUILD_VERSION=96eea1b-mu1 .` |
| 分发 | `docker push` 到 registry；生产机 `docker pull` |

### 4.5 运行切换契约（变更后启动命令）

相对回滚基线（[`spec-infrastructure-derived-image-frontend.md`](spec-infrastructure-derived-image-frontend.md) §4.4）的变化：
1. 镜像 → `ghcr.io/gongzishuye/openhands-custom:96eea1b-mu1`
2. **删除全部 6 个 patch 挂载**（4 py + 2 html，已烤进镜像）

其余保留：env（`SANDBOX_USER_ID=1001`、`INIT_GIT_IN_EMPTY_WORKSPACE=1`、`SANDBOX_LOCAL_RUNTIME_URL`、`OH_PERMITTED_CORS_ORIGINS_0`、`OH_MAX_CONV_PER_SANDBOX=20`、`OH_MAX_NUM_SANDBOXES=15`、`WORKSPACE_MOUNT_PATH`）、端口 `-p 3000:3000`、卷（docker.sock / workspace / `.openhands-state`）、`--add-host`、`gateway.py` 宿主进程。

```bash
docker run -d --restart unless-stopped \
  -e SANDBOX_USER_ID=1001 -e INIT_GIT_IN_EMPTY_WORKSPACE=1 \
  -e SANDBOX_LOCAL_RUNTIME_URL=http://host.docker.internal \
  -e OH_PERMITTED_CORS_ORIGINS_0='http://43.162.100.45:3000' \
  -e OH_MAX_CONV_PER_SANDBOX=20 -e OH_MAX_NUM_SANDBOXES=15 \
  -e WORKSPACE_MOUNT_PATH=/home/mocca/openhands/workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /home/mocca/openhands/workspace:/opt/workspace_base \
  -v /home/mocca/openhands/.openhands-state:/.openhands \
  -p 3000:3000 --add-host host.docker.internal:host-gateway \
  --name openhands-app ghcr.io/gongzishuye/openhands-custom:96eea1b-mu1
```

## 5. Acceptance Criteria

- **AC-000（等价性，本期核心）**：源码构建镜像内 `openhands/` 与 `frontend/build/`，**除 §4.2/§4.3 列出的 6 处已知改动外**，与官方基线镜像 `ghcr.io/openhands/openhands@sha256:369c14ab1ad80dd7dd1d18a00dbbe14869070baba7773a864303ff25174b260b`（commit `96eea1b`）的对应文件 **diff 为空**；且 alice/bob 端到端行为与现网完全一致。此为本期"复现等价"目标的硬判据，未达成不进入后续阶段。
- **AC-001**：Fork 与远程就绪——`git remote -v` 见 `origin`(fork) 与 `upstream`(OpenHands)；`multiuser-deploy` 基于 `96eea1b`。
- **AC-002**：4 个后端补丁在 `multiuser-deploy` 上干净 `git apply`，无 reject；`git diff` 改动量与 §4.2 一致。
- **AC-003**：构建机 `docker build` 成功；镜像 push 到 registry 且可被生产机 pull。
- **AC-004**：生产机用自构建镜像启动，容器 Up 且日志 "Application startup complete"。
- **AC-005**：`docker inspect openhands-app` 挂载中 **6 个 patch 挂载全部不在**（docker.sock/workspace/state 仍在）。
- **AC-006**：`curl http://localhost:3000/login.html` 返回登录页（200）；`curl http://localhost:3000/` 的 index.html 含 X-User-Id 注入脚本。
- **AC-007**：多用户功能不变——alice/bob 登录、会话隔离、每用户独占 sandbox，行为与挂载方案一致。
- **AC-008**：升级演练成功——`git rebase` 到一个更新的上游 commit、重构建、切换后 AC-004~007 仍满足（§9.4）。
- **AC-009**：失败可回退——按 §9.5 回到"官方镜像 + 6 挂载"挂载方案并正常工作。

## 6. Test Automation Strategy

- **Test Levels**：补丁应用验证 + 构建验证 + 部署冒烟 + 端到端手验 + 升级演练。
- **Frameworks**：`git apply --check`、`docker build`、`docker inspect`、`curl`、浏览器；CI 用 GitHub Actions。
- **Test Data**：测试用户 alice/bob；测试后清理会话 + sandbox（见 ../docs/current/architecture.md §9.7）。
- **CI/CD**：构建与 push 在 GitHub Actions；可复用/裁剪上游 `.github/workflows` 的 app 镜像构建。
- **Coverage**：覆盖 §5 全部 AC。
- **Performance**：构建耗时与内存在构建机度量（前端 build 为瓶颈）；运行时性能与官方镜像一致。

## 7. Rationale & Context

### 7.1 实测核实（基线判定依据）
官方镜像 `ghcr.io/openhands/openhands:latest` 的 OCI label：
- `org.opencontainers.image.source = https://github.com/OpenHands/OpenHands`
- `org.opencontainers.image.revision = 96eea1be4774f841c7685e95042c4b5a2afa43e1`
- `org.opencontainers.image.version = main-amd64`（= `:latest` 跟 main 滚动，非 release tag）
- `created = 2026-06-10T20:55:32Z`

commit `96eea1b` 提交信息 "fix: bump agent-server to 1.27.1 for Gemini cache fix (#14751)"，时间 `20:55:14Z`，与镜像构建时间差 18 秒——铁证。该 commit bump 的 agent-server 1.27.1 也正是 sandbox 所用版本。

**版本号陷阱**：包版本 `1.8.0` 由 `poetry-dynamic-versioning` 动态生成，**不是 git tag**；上游真实 tag 形如 `cloud-1.38.0`，与 `1.8.0` 不对应。故基线**以 commit 为准**，不要找 `v1.8.0` tag/分支。

### 7.2 为什么 fork + 定制分支 + rebase
- 改动是源码，升级时 `rebase` 到新上游基线即可，冲突可逐 commit 核对；比每次升级重抠整文件 patch 可维护得多。
- 后端 delta 极小（+100/-10），rebase 几乎无冲突；前端冲突量随定制深度增长，用 GUD-001 的"新增文件 + 开关"纪律压制。

### 7.3 为什么构建/运行分离
上游 Dockerfile 前端段 `npm ci && react-router build` 是内存大户，3.5G/2vCPU 生产机大概率 OOM。CI/大机器构建 + 生产机只 pull，既绕开 OOM 又得到可分发镜像。

### 7.4 为什么功能零差异
源码改动落点 = 现挂载覆盖的同一批文件路径；前端最终产物与现 `frontend-patch/` 一致。app-server 看到的字节一致 → 行为一致。

## 8. Dependencies & External Integrations

- **EXT-001**：`github.com/OpenHands/OpenHands`（upstream 源码）。
- **EXT-002**：`github.com/gongzishuye/OpenHands-custom`（fork）。
- **EXT-003**：容器 registry（建议 GHCR `ghcr.io/gongzishuye/...`）——镜像分发。
- **INF-001**：构建机（GitHub Actions runner 或 ≥8G 机器），Node 22 + Python 3.13 + poetry 2.3.4 + Docker。
- **INF-002**：生产机 Docker（仅 pull + run）。
- **INF-003**：`gateway.py` 宿主进程（:8000）—— 不变，仍需运行。
- **INF-004**：`.openhands-state` 持久卷（含 settings.json 的 GROUP_BY_NEWEST）—— 不变。
- **DAT-001**：`patches/*.py`（4 个）+ `frontend-patch/*.html`（2 个）—— delta 来源与回滚来源，必须保留。
- **PLT-001**：sandbox 镜像 `ghcr.io/openhands/agent-server:1.27.1-python` —— 独立，不变。

## 9. Examples & Edge Cases

### 9.1 阶段 0 — Fork 与分支初始化

```bash
git clone git@github.com:gongzishuye/OpenHands-custom.git
cd OpenHands-custom
git remote add upstream https://github.com/OpenHands/OpenHands.git
git fetch upstream
git checkout -b multiuser-deploy 96eea1be4774f841c7685e95042c4b5a2afa43e1
```

### 9.2 阶段 1 — 后端 delta 抽补丁并入源码

```bash
# 在补丁仓（含 patches/ 与从基线镜像抠出的 orig_*）生成 4 个 unified diff
for f in default_user_auth docker_sandbox_service \
         sql_app_conversation_info_service live_status_app_conversation_service; do
  diff -u orig_$f.py patches/$f.py > $f.patch
done
# 在 OpenHands-custom / multiuser-deploy 上 apply（路径按 §4.2 对应）
git apply --check <补丁>     # 先 dry-run，确认干净
git apply <补丁>
git add -A && git commit -m "backend: multi-user isolation (user_auth/sandbox/conversation)"
```

### 9.3 阶段 3 — 构建（CI 或大机器）+ push

```bash
docker build -f containers/app/Dockerfile \
  -t ghcr.io/gongzishuye/openhands-custom:96eea1b-mu1 \
  --build-arg OPENHANDS_BUILD_VERSION=96eea1b-mu1 .
docker push ghcr.io/gongzishuye/openhands-custom:96eea1b-mu1
```
生产机：`docker pull ghcr.io/gongzishuye/openhands-custom:96eea1b-mu1` → 按 §4.5 启动。

### 9.4 上游升级流程（常态化）

```bash
git fetch upstream
# 选定新基线（某 commit 或 tag），把定制 commit 栈搬过去
git rebase --onto <新基线> 96eea1be4774f841c7685e95042c4b5a2afa43e1 multiuser-deploy
# 解冲突（后端通常无；前端按需）→ 构建新 tag（如 <新基线短hash>-mu2）→ 生产机 pull + 切换 → 验证 AC-004~007
```

### 9.5 回滚

| 层级 | 操作 |
|---|---|
| 镜像级 | `docker rm -f openhands-app` → 用上一个可用 tag（如 `96eea1b-mu1`）重启 |
| 方案级 | 回到"官方镜像 + 6 挂载"：执行 [`spec-infrastructure-derived-image-frontend.md`](spec-infrastructure-derived-image-frontend.md) §4.4 完整命令（前提：`patches/` 与 `frontend-patch/` 未删，CON-003） |

### 9.6 边界情况

| 情况 | 处理 |
|---|---|
| 补丁 `git apply` 有 reject | 说明基线选错（非 `96eea1b`）；核对 commit 后重试 |
| 前端构建 OOM | 违反 CON-002——挪到 CI/大机器；不要在生产机 build |
| 上游大改了 4 文件之一 | rebase 时手动解冲突；delta 小，对照 §4.2 重打 |
| fork 名用了 `openhands` | 与补丁仓冲突（CON-005）；用 `OpenHands-custom` |
| 误把镜像 build 进 :latest 漂移 | tag 固定含上游基线 hash（GUD-003），不要依赖 `latest` |
| `agent-server` 版本需同步 | 本方案不碰；如上游 bump，按现架构单独处理 sandbox 镜像 |

## 10. Validation Criteria

1. `git remote -v` 与 `git log multiuser-deploy` 显示正确远程与基线 commit。
2. 4 个补丁 `git apply --check` 全通过；`git diff --stat` 改动量符合 §4.2。
3. 构建机 `docker build` 成功，镜像 push 且生产机可 pull。
4. 生产机容器 Up，日志 "Application startup complete"。
5. `docker inspect openhands-app` 无 6 个 patch 挂载（AC-005）。
6. `curl /login.html` 返回登录页；`curl /` 含 X-User-Id 注入。
7. 浏览器 alice/bob 登录、隔离、各自 sandbox 正常。
8. 升级演练（§9.4）后上述 4~7 仍满足。
9. 回滚（§9.5）可恢复到挂载方案并正常工作。

## 11. Related Specifications / Further Reading

- [`spec-infrastructure-derived-image-frontend.md`](spec-infrastructure-derived-image-frontend.md) —— 派生镜像（仅前端进镜像、后端仍挂载）+ 当前架构回滚基线（§4.4），本方案的轻量回退选项与回滚来源。
- [`spec-design-frontend-login-userid.md`](spec-design-frontend-login-userid.md) —— 前端登录 + X-User-Id 注入的设计逻辑（本方案改其"部署/构建手段"，不改其逻辑）。
- [`plan-frontend-login-userid.md`](plan-frontend-login-userid.md) —— 前端登录实施计划。
- `../docs/current/architecture.md` —— 多用户方案全貌与当前运行架构（gateway / sandbox 亲和 / 隔离）。
- 上游 `containers/app/Dockerfile`（commit `96eea1b`）—— 构建参照。
