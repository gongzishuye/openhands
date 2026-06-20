# OpenHands 多用户部署方案（修订版）

> **状态：🗄️ 搁置（未采用的方向）。** 本方案走"官方 remote runtime 当 Worker"，但现网最终采用的是 app-server + agent-server + 自建 gateway（见 [`../current/architecture.md`](../current/architecture.md)）。保留作未来扩到多机/30 人时的参考。

> 目标：30 人通过统一入口使用 OpenHands Web GUI，15 个 sandbox 可并发，第 16 个排队，单用户单任务，workspace 不串。
> 修订要点（相对初版）：
> 1. **Gateway 降级为薄控制面**——不再自己 `docker run`，改由**官方 remote runtime 当 Worker**，Gateway 只做鉴权/配额/排队/身份映射 + 透传。
> 2. 把 `RUNTIME=remote` 的契约从源码里钉死，阶段 0 spike 收窄。
> 3. 补两块初版没解决的硬骨头：**排队的前端感知**、**30 用户身份隔离**。
> 4. 修掉版本号不一致（pin 到 1.27.x 一致三元组）、阶段 1 明确"不是现在 3.5G 这台机"。

---

## 0. 核心原则

- **Web 不直接操作 Docker**，只走 `RUNTIME=remote` 找 Gateway。
- **Gateway 不自己拉容器**，只做控制面（鉴权、配额、排队、身份映射），把 `/start` `/resume` `/list` 透传给官方 remote runtime。
- **Worker = 官方 OpenHands remote runtime**，负责容器生命周期、端口分配、回收——这些不重造。
- 这样阶段 3 把 Worker 抽到第二台机器时，Web 和 Gateway 几乎不动，只换 Worker 地址。

```
Cloudflare Access (身份入口)
   │  Cf-Access-Jwt-Assertion
   ▼
OpenHands Web GUI (RUNTIME=remote, SANDBOX_REMOTE_RUNTIME_API_URL=Gateway)
   │
   ▼
Runtime Gateway（薄控制面，本方案要写的部分）
   1. 鉴权（内部 API key + 校验 CF JWT）
   2. 配额（active < MAX_ACTIVE_SANDBOXES）
   3. 单用户限制（MAX_SANDBOX_PER_USER=1）
   4. 排队 + 状态
   5. 身份映射（CF 用户 → OpenHands user_id）
   │  透传 /start /resume /list
   ▼
官方 remote runtime（Worker，不写，直接跑）
   └─ sandbox 容器（端口/回收/agent-server URL 全由它管）
```

---

## 1. RUNTIME=remote 契约（已从源码确认）

Web 的 `RemoteSandboxService` 向 `SANDBOX_REMOTE_RUNTIME_API_URL` 发的就是这套（来自 `remote_sandbox_service.py`），**Gateway 只需满足这个契约即可对接 Web**：

```
POST /start
  body: { image, working_dir, environment, session_id,
          resource_factor, run_as_user, run_as_group, fs_group,
          [runtime_class="sysbox-runc"] }
  resp: { url, status, session_api_key, runtime_id }
        # url = 可连接的 agent-server 地址；Web 之后直连 url/api/conversations/...

POST /resume
  body: { runtime_id }
  resp: { session_api_key, ... }          # resume 会换发新 key（旧 key 作废）

GET  /list
  header: X-API-Key
  resp: { runtimes: [ {runtime_id, session_id, status, url, ...} ] }

鉴权：X-API-Key 头（= SANDBOX_API_KEY）
状态枚举（Web 认）：running / paused / stopped   ← 注意：原生没有 "queued"
```

> 结论：契约小而清晰。**阶段 0 的 spike 不再是"能不能对接"，而是"官方 remote runtime 能否自托管 + 版本对齐 + Gateway 透传跑通"**。

### 版本必须对齐（强约束，现网已查实）
`/start` 契约是**版本耦合**的，Web / agent-server / remote runtime 要对齐版本。现网实测（2026-06-17 进容器直查，非记忆）：

```
openhands-app（Web GUI + app_server）   镜像 ghcr.io/openhands/openhands:latest
  digest sha256:369c14ab...260b（:latest 会漂移，必须锁！）
  app_server             = 1.8.0   ← /app/openhands/app_server/version.py
  openhands-sdk          = 1.27.0  ← pip 包（/.venv/site-packages）
  openhands-agent-server = 1.27.0  ← pip 包
  openhands-tools        = 1.27.0  ← pip 包

agent-server 沙箱（oh-agent-server-*）   镜像 ghcr.io/openhands/agent-server:1.27.1-python
  digest sha256:991cd159...2f3a（容器内无 pip、import openhands→ModuleNotFoundError，内部版本不可查，以 tag 1.27.1 为准）
```

⚠️ **更正初版误判**：`openhands.__version__=1.8.0` 命中的是 `/app/openhands/__init__.py`（应用源码，`from openhands.app_server.version import __version__`，注释 backward compat）。**1.8.0 是 app_server 这个独立组件的号，不是"包版本出错"**；SDK 真实版本是 1.27.0 系列。它们是**并行组件、不是同一物的两套号**——故非 bug 级 skew。

**阶段 0 唯一要验的 skew**：SDK `1.27.0` ↔ 沙箱镜像 `1.27.1`，差一个 patch。这个 patch 会不会让 `/start` 协议不兼容才是重点（docker-local 现能跑通，remote 协议未验）。**第一件事**：禁 `:latest`、改用 digest 锁死镜像（漂移会悄悄改四元组），再确认 SDK↔沙箱↔remote runtime 协议兼容。

remote runtime 镜像：之前说 `ghcr.io/openhands/runtime:*`、`ghcr.io/all-hands-ai/runtime:*` 不存在——**查错 registry 了**。官方 runtime 镜像在自家 registry `docker.all-hands.dev/all-hands-ai/runtime:<tag>`，且自托管 remote runtime 是官方支持的（见 issue #6780）。确切 tag 仍在阶段 0 与现网 SDK 1.27.0 对齐确认。**禁止混版本、禁 `:latest`。**

---

## 2. OpenHands 原生 vs 自建边界（先分清谁干啥）

| 能力 | 用 OpenHands 原生 | 还是要自建 |
|---|---|---|
| 容器生命周期、端口分配、回收 | ✅ 官方 remote runtime | ❌ 别造 |
| Web ↔ runtime 协议（/start 等） | ✅ 原生 `RUNTIME=remote` | ❌ 别造 |
| **15 并发全局上限 + 排队** | ❌ 原生 `max_num_sandboxes` 在 docker/remote 路径 `pause_old_sandboxes(<=0)` 会抛 `ValueError`，做不了 | ✅ Gateway |
| **单用户单任务** | ❌ 原生 `max_num_conversations_per_sandbox` 写死 20、不可配 | ✅ Gateway |
| **30 用户身份隔离** | ⚠️ OSS 默认单用户（`DefaultUserAuth` 共享） | ✅ Gateway + CF Access |
| workspace 隔离 | ✅ runtime 按 session_id 隔离 | — |
| 监控/告警 | — | ✅ 自建 |

> `MAX_ACTIVE_SANDBOXES`、`MAX_SANDBOX_PER_USER` 是 **Gateway 自己定义的环境变量**，不是 OpenHands 参数。

---

## 3. 阶段 0：Spike（半天～1 天，决定后续形态）

产出三条结论，任何一条不满足就回来改方案：

```
1. 官方 remote runtime 能在测试机上自托管起来（单实例，docker 跑通）
2. Web 用 RUNTIME=remote 指向它，能创建第一条会话、agent 能回话
3. 确认三者版本对齐；记录 /start 返回的 url/session_api_key 形态
```

验收：

```
Web → Gateway(透传, 不加任何控制) → 官方 runtime → 1 个 sandbox
能创建会话、能跑命令、Web 不断连
```

> 这一步 Gateway 可以先写成一个"无脑透传 + 打日志"的反向代理，只为验证契约和版本。

---

## 4. 阶段 1：同机最小闭环（在测试机，不是 3.5G 这台）

> ⚠️ 现在这台腾讯云 3.5G/可用 ~640M，**一个 sandbox 就快撑满**，跑不了多组件 + 压测。阶段 1 换一台 **8G+ 内存**的测试机。

同机跑：

```
openhands-web
runtime-gateway（薄控制面）
redis
官方 remote runtime（= Worker）
sandbox 容器（runtime 自动起）
```

目录：

```
/opt/agentx/
  compose.yaml
  gateway/
  data/
    redis/
    workspaces/{user-a,user-b}/
    logs/
```

Gateway 第一版只做这 5 件**控制面**的事（注意：不含 docker run / 端口分配）：

```
1. 鉴权：内部 API key + 校验 CF JWT
2. 并发控制：active sandbox < MAX_ACTIVE_SANDBOXES，超额进入 FIFO 队列
3. 透传：/start /resume /list → 官方 runtime
4. 回收策略下发：idle timeout / max lifetime（透传给 runtime 的参数）
5. 状态查询：running / queued / failed / stopped
```

Redis 只存简单状态：

```
active_sandboxes            (set)
queue                       (list, FIFO)
sandbox:{id}                (hash)
user:{email}:active_sandbox (string)
```

第一期策略：

```
MAX_ACTIVE_SANDBOXES=15
MAX_SANDBOX_PER_USER=1
FIFO 队列
```

---

## 5. 两个比想象中难的点（必须先想清楚再动手）

### 5.1 排队的前端感知
Web 调 `/start` 是**同步等结果**的，原生状态枚举只有 running/paused/stopped，**没有 queued**。第 16 个任务"进队列"有两种实现，各有坑：

```
方案 A（阻塞）：Gateway 把 /start 挂住，等有槽再返回
  → 坑：httpx/uvicorn 有超时；Web 侧 startup_grace_seconds 也会判失败
方案 B（异步，推荐）：Gateway 立即返回一个 pending runtime（status=pending）
  + Web 用已有的 poll_agent_servers 轮询 /list
  → 坑：需扩展状态映射让 Web 认 "pending"；前端要有"排队中 N 人 ahead"的提示
```

**先确认**：Web 端 `/start` 的超时时间、重试行为，以及 `poll_agent_servers` 能否承载 pending 状态。再决定 A/B。别等压测才发现前端体验崩。

### 5.2 30 用户身份隔离
OSS 默认 `DefaultUserAuth` 是**单用户共享**（所有人同身份、共享 workspace/secrets/会话）。要做到每人独立：

```
CF Access 把身份放进 Cf-Access-Jwt-Assertion（JWT 头）
   → Gateway（或 Web 前一层）解 JWT 拿到 email/sub
   → 映射成 OpenHands user_id
   → Web 按该 user_id 隔离会话/secrets/profiles
```

**要先验证**：OpenHands Web 能否接受外部注入的 user_id（而不是它自己生成的共享 id）。这是仅次于协议的第二难集成，初版基本没写，**建议在阶段 0 一起探明**，别拖到压测。

---

## 6. 阶段 2：同机梯度压测

按梯度，别直接上 15：

```
1  sandbox：验证功能
3  sandbox：验证端口、workspace 隔离
6  sandbox：验证内存和清理
10 sandbox：验证并发稳定性
15 sandbox：最终压力线
```

每个 sandbox 验证：

```
能执行命令
能读写独立 workspace
不会抢同一个端口
任务结束能删除容器
workspace 不串用户
Redis 状态正确
OpenHands Web 不断连
排队（第 N+1 个）行为符合 5.1 选定的方案
```

监控至少看：

```
CPU / 内存 / 磁盘
Docker container 数
sandbox 创建耗时
任务失败率
队列长度 / 平均等待时间
```

---

## 7. 阶段 3：把 Worker（官方 runtime）抽到第二台机器

测试通过后，新增一台 Worker 机：

```
24 vCPU / 96GB RAM / 1TB NVMe   # 15 并发含 chromium，96G 宽裕
Ubuntu 22.04/24.04 + Docker Engine
只跑官方 remote runtime + sandbox
```

迁移二选一：

```
简单：Gateway 用 SSH Docker context 控制远程
      DOCKER_HOST=ssh://worker-user@worker-ip → 远程 docker daemon
      优点快；缺点 Gateway 拿远程 docker 控制权，安全边界要管好

推荐：Worker 上跑官方 runtime，Gateway 经 HTTPS/mTLS 调它
      优点边界清晰、可健康检查/容量上报/日志收集
      缺点多一层网络
```

> 因为 Worker 就是官方 runtime（不是自造 driver），"local docker / remote docker 两种 driver"这件事**自然由 runtime 实例位置决定**，Gateway 不用写两套 driver——只换 `SANDBOX_REMOTE_RUNTIME_API_URL` 指向远端 runtime。

---

## 8. 阶段 4：网络与 URL 统一

同机时 sandbox endpoint 可能是 `http://localhost:8xxx`；拆出去后是 `http://worker-private-ip:8xxx`。**从第一天起就别在代码里写死 localhost**。

Gateway 统一对外返回：

```json
{
  "sandbox_id": "sbx_123",
  "agent_server_url": "http://worker-01.internal:8123",
  "workspace_id": "...",
  "status": "running"
}
```

迁移时只改 worker 地址，Web 不动。

---

## 9. 阶段 5：安全与隔离

最低要求：

```
Worker 不暴露公网
Gateway ↔ Worker 走内网 / VPN / Tailscale（mTLS）
每个 sandbox 独立 workspace
每个 sandbox 加 Docker labels + memory/cpu 限制
禁止 host network           ← 规避之前 host 网络单端口那一串坑
禁止复用同一个 workspace
磁盘超阈值拒绝新任务
sandbox session_api_key 仅返回一次；resume 换发新 key
```

sandbox 容器 label 示例：

```
agentx.sandbox_id=sbx_123
agentx.user=alice@example.com
agentx.created_at=...
agentx.expires_at=...
```

---

## 10. 执行顺序

```
1. 钉死版本（现网四元组：app_server=1.8.0 / SDK 三件套=1.27.0 / 沙箱镜像=1.27.1；1.8.0 是组件号非 skew，真正待验是 SDK 1.27.0↔沙箱 1.27.1 的 patch 差；改 digest 锁镜像、禁 :latest；runtime 镜像在 docker.all-hands.dev，tag 阶段0 确认）
2. 阶段 0 spike：官方 runtime 自托管 + 透传 Gateway + 首条会话
   （同时探明 5.1 超时/轮询、5.2 身份注入两件事）
3. Gateway 加鉴权 + 配额 + FIFO 队列（选定 5.1 方案 A/B）
4. 同机 compose：Web + Gateway + Redis + 官方 runtime
5. 跑 1 个 sandbox 完整会话
6. 梯度压测 1→3→6→10→15
7. Worker 抽到第二台机（mTLS）
8. 回归 1/3/6/15 并发
9. 接 Cloudflare Access 做内测入口（身份隔离端到端打通）
```

---

## 11. 通过标准

```
30 人能经 CF Access 登录入口，身份互不串
15 个 sandbox 同时运行
第 16 个任务按 5.1 方案正确排队（前端可感知）
单用户不能开多个并发任务
任务结束后容器自动删除
workspace 不串用户
Worker 重启后 Gateway 能恢复/清理状态
磁盘超阈值拒绝新任务
Web GUI 能稳定连接 sandbox
Web / agent-server / runtime 版本三元组一致
```

---

## 12. 相对初版的改动清单

```
✅ Gateway 不再 docker run，改为透传 + 控制面（省掉一整层自造）
✅ Worker = 官方 remote runtime（不写 driver）
✅ 补 RUNTIME=remote 契约（/start /resume /list + X-API-Key）
✅ 版本：现网四元组 app_server=1.8.0 / SDK 三件套=1.27.0 / 沙箱镜像=1.27.1（1.8.0 是 app_server 组件号、非 skew；真正待验是 SDK 1.27.0↔沙箱 1.27.1 patch 差）；阶段0 改 digest 锁镜像、禁 :latest；runtime 镜像在 docker.all-hands.dev（之前 ghcr.io 找错 registry）、自托管见 issue #6780
✅ 阶段 1 明确换测试机（不是 3.5G 这台）
✅ 新增 §5：排队前端感知 + 身份隔离两块实现路径
✅ 新增 §2：原生 vs 自建边界表（明确 MAX_ACTIVE_SANDBOXES 是 Gateway 的）
✅ 安全：强调禁止 host network、key 一次性返回
♻️ 保留：两阶段拆分、梯度压测、Web 不碰 Docker、URL 不写死 localhost、通过标准
```
