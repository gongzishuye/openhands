# OpenHands 单端口 Gateway 多并发 Sandbox 方案

> **状态：✅ 当前权威（现网实际运行的架构）。** 其余 `docs/` 下文档为历史/搁置参考，见 [`../../README.md`](../../README.md)。

> 目标：多用户/多会话并发 sandbox，**公网只开一个端口（8000）**，加 sandbox 不加端口、可扩展。
> 环境：腾讯云 `43.162.100.45`，3.5G/2vCPU；`openhands-app`(1.8.0) + `agent-server`(1.27.1)。
> 状态（2026-06-17）：3 个 sandbox 并发 + gateway 路由已跑通；新 sandbox 启动窗口用重试解决。
> 状态（2026-06-17 更新）：**多用户已上线**——每个用户独占一个 sandbox、会话互不可见（详见 §9）。
> 状态（2026-06-17 最新）：**身份注入已从 `user_proxy.py` 改为前端登录页**（§9.10），**浏览器端到端已验证**（alice/bob + 123456 均能登录）。app-server 直接对外 :3000。前端 `login.html`（alice/bob=123456）登录后由注入脚本给每个 REST 请求加 `X-User-Id`。
> ⚠️ **`user_proxy.py` 已于 2026-06-18 删除**（非 git 仓库，不可恢复）。原"回滚到反向代理"的路径已失效；如需回滚到 `?user=` cookie 方案，须先重建该文件。
> 本文档取代早期的 [`../history/bridge-gateway.md`](../history/bridge-gateway.md)（那个的 path 路由方案已被推翻）。

---

## 1. 架构

```
浏览器 ──3000──► openhands-app (Web GUI + app-server)
浏览器 ──8000──► gateway ──按 session_api_key──► sandbox_A (随机端口, 内部)
                                          └──► sandbox_B (随机端口, 内部)
                                          └──► sandbox_C ...
```

- **app (3000)**：Web GUI + app-server，挂着 docker.sock 自己拉起 `oh-agent-server-*` sandbox（**bridge 模式**，随机端口）
- **gateway (8000)**：唯一公网端口。docker inspect 所有 sandbox 建 `session_api_key → 随机端口` 映射，按请求里的 key 路由
- **sandbox**：端口随机、互不冲突、天然并发；公网不可达（只在服务器内部，gateway 访问 `localhost:随机端口`）

**核心**：前端永远连 8000（gateway），sandbox 的随机端口由 gateway 动态映射。加 sandbox 不加公网端口。

---

## 2. 关键认知（踩坑得来，决定方案形态）

| # | 事实 | 影响 |
|---|---|---|
| 1 | host 网络模式：sandbox 固定占 8000，第二个 `bind failed` | host 模式**只能 1 个 sandbox**，不能并发 |
| 2 | bridge 模式：`_find_unused_port()` 给每个 sandbox 随机端口 | **bridge 才能多 sandbox 并发** |
| 3 | `OH_SANDBOX_CONTAINER_URL_PATTERN` env **不被读取**（Injector Field 无 env 绑定，`model_config` 只 frozen） | 改 pattern 没用，得改代码 |
| 4 | `max_num_conversations_per_sandbox` 默认 **20** | 默认 20 个会话复用 1 个 sandbox，多会话≠多 sandbox |
| 5 | 前端连 sandbox 用 `window.location.host + url.port`，**丢 path**（`v1-conversation-service.api`：`${s}:${a.port}`） | **path 路由 gateway 不可行**（`/p/{port}` 收不到请求） |
| 6 | `session_api_key` 在 sandbox 容器 env `OH_SESSION_API_KEYS_0`；前端每个请求都带（HTTP header `X-Session-API-Key` / WS query `session_api_key`） | **靠 session_api_key 路由可行** |

结论：前端用 host+port 直连 → 只能控制 port；让 port 固定 8000（给前端）+ sandbox 随机（内部）+ gateway 按 key 路由 = 单端口多并发。

---

## 3. 改动（3 部分）

### 3.1 patch `docker_sandbox_service.py` —— AGENT_SERVER url 固定 8000

让前端连 8000（gateway）；sandbox 实际端口仍随机（gateway 映射）。只改 bridge 分支的 url 生成（`docker_sandbox_service.py` ~line 204）：

```python
# 原代码
if matching_port:
    url = self.container_url_pattern.format(port=host_port)

# 改为：AGENT_SERVER 对外固定 8000（gateway），其余端口保持真实值
if matching_port:
    display_port = 8000 if matching_port.name == AGENT_SERVER else host_port
    url = self.container_url_pattern.format(port=display_port)
```

> pattern 默认 `http://localhost:{port}`，`format(port=8000)` → `localhost:8000` → app 内部替换成 `host.docker.internal:8000` → 前端用 `window.location.host` 连 `43.162.100.45:8000`。

### 3.2 patch `live_status_app_conversation_service.py` —— max 可配

Injector 不读 env，给 `max_num_conversations_per_sandbox` 加 `os.getenv` default_factory（`os` 已 import 在 line 4，Field 在 ~line 2117）：

```python
max_num_conversations_per_sandbox: int = Field(
    default_factory=lambda: int(os.getenv('OH_MAX_CONV_PER_SANDBOX', '20')),
    description='The maximum number of conversations allowed per sandbox',
)
```

设 `OH_MAX_CONV_PER_SANDBOX=1` → **每个会话独占一个 sandbox**。

### 3.3 `gateway.py` —— session_api_key 路由

完整文件：`/home/mocca/openhands/gateway.py`。核心逻辑：

- **建映射**：后台线程每 3s `docker inspect` 所有 `oh-agent-server-*`，从 env `OH_SESSION_API_KEYS_0` + `8000/tcp` 的 HostPort 建 `{session_api_key: 随机端口}`，并记录**最近创建**的 sandbox port（`_LATEST_PORT`）
- **HTTP 路由**：提取 `X-Session-API-Key` → 查映射 → 转发 `localhost:端口`
- **WS 路由**：`/sockets/*` 提取 query `session_api_key` → WS 双向转发
- **`/health`（无 key）**：转发到 `_LATEST_PORT`（app 等的就 是刚建的那个）+ 重试——**真 check，不假返回 200**
- **upstream 重试**：转发失败重试 30 次 × 1s（等新 sandbox 的 agent-server 启动，本机慢要十几秒）
- **OPTIONS 预检**：返回 CORS 头（浏览器跨端口预检不带 key，不能 502）

---

## 4. 部署

### 4.1 准备 patch 文件

```bash
mkdir -p /home/mocca/openhands/patches
# 从容器拷出原文件，应用 §3.1 §3.2 的改动，放这里：
docker cp openhands-app:/app/openhands/app_server/sandbox/docker_sandbox_service.py \
  /home/mocca/openhands/patches/docker_sandbox_service.py
docker cp openhands-app:/app/openhands/app_server/app_conversation/live_status_app_conversation_service.py \
  /home/mocca/openhands/patches/live_status_app_conversation_service.py
# (编辑这两个文件应用改动)
```

### 4.2 起 app（挂载两个 patch + max=1）

```bash
docker rm -f $(docker ps -aq --filter name=oh-agent-server) openhands-app
docker run -d --restart unless-stopped \
  -e SANDBOX_USER_ID=1001 \
  -e INIT_GIT_IN_EMPTY_WORKSPACE=1 \
  -e SANDBOX_LOCAL_RUNTIME_URL=http://host.docker.internal \
  -e OH_PERMITTED_CORS_ORIGINS_0='http://43.162.100.45:3000' \
  -e OH_MAX_CONV_PER_SANDBOX=1 \
  -e WORKSPACE_MOUNT_PATH=/home/mocca/openhands/workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /home/mocca/openhands/workspace:/opt/workspace_base \
  -v /home/mocca/openhands/.openhands-state:/.openhands \
  -v /home/mocca/openhands/patches/docker_sandbox_service.py:/app/openhands/app_server/sandbox/docker_sandbox_service.py:ro \
  -v /home/mocca/openhands/patches/live_status_app_conversation_service.py:/app/openhands/app_server/app_conversation/live_status_app_conversation_service.py:ro \
  -p 3000:3000 --add-host host.docker.internal:host-gateway \
  --name openhands-app ghcr.io/openhands/openhands:latest
```

云 SG 放行 **3000 + 8000**（8000 给 gateway）。

### 4.3 起 gateway

```bash
pip3 install --quiet docker aiohttp   # 宿主依赖
python3 /home/mocca/openhands/gateway.py   # 前台跑或 nohup/& / systemd
```

> ⚠️ **不要用 `pkill -f gateway.py` 重启**——命令行里含 "gateway.py" 会把执行命令的 bash 自己也杀掉（exit 144）。用 PID `kill`、`run_in_background`、或 systemd 管理。

---

## 5. 验证

```bash
# 1. 多 sandbox 并发（开 N 个会话 → N 个 sandbox）
docker ps --filter name=oh-agent-server --format 'table {{.Names}}\t{{.Status}}'

# 2. gateway 映射（每个 sandbox 一条 key→port）
tail -f /tmp/gateway.log | grep '\[map\]'
# 预期：[map] N key(s): <key8>:<port>, ... latest=<最新port>

# 3. 前端连 8000（F12 Network 看 ws/http 请求端口）
# 预期：ws://43.162.100.45:8000/sockets/events/...  (固定 8000，不是随机端口)

# 4. agent 能回话 = 端到端通
```

---

## 6. 踩坑记录（按出现顺序）

| 坑 | 现象 | 根因 | 修复 |
|---|---|---|---|
| host 模式单 sandbox | 第二个 sandbox `bind 8000 failed` | host 网络固定端口冲突 | 切 bridge |
| pattern env 不生效 | 改 `OH_SANDBOX_CONTAINER_URL_PATTERN` 前端还连随机端口 | Injector 不读 env | patch url 生成代码 |
| 多会话=1 sandbox | 开多个会话只有 1 个 sandbox 容器 | `max_num_conversations_per_sandbox=20` 复用 | patch + `=1` |
| path 路由 gateway | `/p/{port}` gateway 收不到请求 | 前端丢 path，用 host+port 直连 | 改用 session_api_key 路由 |
| `/health` 假 200 | max=1 时新 sandbox 502，max=20 没事 | gateway 对 /health 假返回，app 误判 ready，新 sandbox 没起好就调 | gateway /health 真转发到 latest sandbox |
| 启动窗口 502 | `upstream error: Connection reset` | 新 sandbox agent-server 启动慢，5s 重试不够 | 重试窗口拉到 30s |
| OPTIONS 502 | 浏览器 CORS 预检失败 | OPTIONS 不带 key，gateway 502 | gateway 对 OPTIONS 返回 CORS 头 |
| `pkill -f gateway.py` exit 144 | 重启 gateway 时 bash 被杀 | pkill 匹配到命令行含 gateway.py 的 bash | 用 PID / run_in_background / systemd |
| WS `Cannot write to closing transport`（**前端报 "Failed to connect to server"**，2026-06-18 定位） | gateway WS 日志持续刷该错；前端弹"Failed to connect to server"提示但功能正常 | `handle_ws` 用 `asyncio.gather` 双向 pump，一端关闭后另一端仍 `send` → 异常关闭 client WS → 浏览器 socket.io 判定连接出错弹提示后重连。父子（plan 模式）会话会开**第二条 WS**，放大了该问题 | 重写 `handle_ws`：`asyncio.wait(FIRST_COMPLETED)` + `finally` 关闭对端 + `send` 前查 `dst.closed` + 双向 `heartbeat=30`。改后 ws 错误归零 |

---

## 7. 回滚（恢复现网单 sandbox host 模式）

```bash
docker rm -f $(docker ps -aq --filter name=oh-agent-server) openhands-app
docker run -d --restart unless-stopped \
  -e SANDBOX_USER_ID=1001 -e INIT_GIT_IN_EMPTY_WORKSPACE=1 \
  -e AGENT_SERVER_USE_HOST_NETWORK=true \
  -e SANDBOX_LOCAL_RUNTIME_URL=http://host.docker.internal \
  -e OH_SANDBOX_CONTAINER_URL_PATTERN='http://43.162.100.45:{port}' \
  -e OH_PERMITTED_CORS_ORIGINS_0='http://43.162.100.45:3000' \
  -e WORKSPACE_MOUNT_PATH=/home/mocca/openhands/workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /home/mocca/openhands/workspace:/opt/workspace_base \
  -v /home/mocca/openhands/.openhands-state:/.openhands \
  -p 3000:3000 --add-host host.docker.internal:host-gateway \
  --name openhands-app ghcr.io/openhands/openhands:latest
# 停 gateway（用 PID kill，别 pkill -f）
```

---

## 8. 已知限制 / 待办

- **本机 3.5G 内存**：并发数受内存限（实测每 sandbox 空闲 ~75M，跑任务会涨）。要 15 并发得迁 8G+ 机器（patch 文件随挂载迁移即可）。
- **gateway 单点**：gateway 挂了所有会话断。生产建议 systemd 自启 + 健康检查。
- **gateway 有状态**：靠 docker inspect 维护 key→port 映射（3s 刷新 + 请求时按需刷新）。
- **`max_num_sandboxes` 默认 5**：超过 5 个 sandbox 会 pause 最早的。要更多并发调大（同样需 patch，因 Injector 不读 env）。
- **升级 `openhands:latest`**：两个 patch 要重新 diff 应用（文件路径稳定的话冲突不大）。
- **session_api_key 路由依赖 key 在请求里**：HTTP header / WS query 都带，已验证。若未来 OpenHands 改鉴权方式，gateway 提取逻辑要跟着改。

---

## 9. 多用户支持（每个用户独占一个 sandbox）

> 目标：前端带一个 user_id，**每个用户独占一个 sandbox，该用户的所有 conversation 都跑在这个 sandbox 里**；用户之间会话互不可见。
> 状态（2026-06-17）：已上线并端到端验证（见 §9.6）。

### 9.1 架构（叠加在 §1 的 gateway 方案上）

```
浏览器 ──?user=alice──► user_proxy(3000) ──种 cookie oh_user=alice──► ┐
                                                                       │ 每请求注入 X-User-Id
                                                                       ▼
                                            app-server(3010, 内部)  get_user_id()=alice
                                                  │  GROUP_BY_NEWEST 复用该用户的 sandbox
                                                  ▼
                                            sandbox_A (label oh_user_id=alice)
浏览器 ──8000──► gateway ──按 session_api_key──► sandbox_A / sandbox_B ...
```

- **user_proxy (3000，公网)**：aiohttp 反代，把 `?user=<id>` 变成 cookie + `X-User-Id` 头。**前端 bundle 不动**。
- **app-server (3010，内部)**：唯一被 patch 的"身份总开关"在 `get_user_id()`，读 `X-User-Id`。
- **gateway (8000)**：**不用改**。它按 `session_api_key` 路由，每个 sandbox 有自己的 key，天然 per-sandbox。

### 9.2 关键认知（踩坑得来，决定方案形态）

| # | 事实 | 影响 |
|---|---|---|
| 1 | OpenHands **已内置** per-user 亲和性：`_find_running_sandbox_for_user` 按 `sandbox.created_by_user_id == user_id` 过滤 + `GROUP_BY_NEWEST` 复用 | 不用自己写分配器，只需打通身份 |
| 2 | OSS 模式 `DefaultUserAuth.get_user_id()` **恒返回 None**（`default_user_auth.py`） | 这是唯一缺失的身份入口，patch 它读 `X-User-Id` |
| 3 | `settings.json` 默认就是 `GROUP_BY_NEWEST`；`max_num_conversations_per_sandbox` 已 env 化(=20) | 亲和策略不用配 |
| 4 | **`DockerSandboxService` 从不给 sandbox 打用户标记**：`_container_to_sandbox_info` 硬编码 `created_by_user_id=None`，`start_sandbox` 也不传用户 | 内置亲和性**形同虚设**（全 None 永远匹配不到）→ 必须自己用 docker label 把用户串起来 |
| 5 | 会话表 `StoredConversationMetadata` **没有 user_id 列**（注释说挪到 SaaS 才有的 `ConversationMetadataSaas`，OSS 不存在） | 会话↔用户唯一联系是 `conversation.sandbox_id → sandbox.created_by_user_id`，按 sandbox_id 过滤实现隔离 |
| 6 | 会话 list/search 端点默认**不按用户过滤**（OSS 单租户） | 要 patch 才能隔离侧栏 |
| 7 | **光过滤 list 不够**：`GET /app-conversations?ids=` 按 id 直取、`PATCH/DELETE /{id}`、`/{id}/file\|git\|skills\|hooks\|download` 子路由都**不校验归属** | 知道对方 conversation id 即可越权读/改/删 → 必须在取数 service 方法统一卡归属（§9.6） |

结论：身份(patch `default_user_auth.py`) + sandbox 打标(label `oh_user_id`) + 放开上限(`max_num_sandboxes`) + 前置代理(`user_proxy.py`) + 会话按 sandbox_id 过滤 + 会话级取数强制归属(§9.6)。

### 9.3 改动（5 个文件，都在 `/home/mocca/openhands/`）

**① `patches/default_user_auth.py`（新，身份总开关）** —— 让 `get_user_id()` 返回 `X-User-Id` 头：
```python
@dataclass
class DefaultUserAuth(UserAuth):
    _user_id: str | None = None        # ← 新增字段
    _settings: ...
    async def get_user_id(self): return self._user_id     # ← 改
    @classmethod
    async def get_instance(cls, request):
        uid = request.headers.get('X-User-Id')            # ← 改
        return DefaultUserAuth(_user_id=uid)
```

**② `patches/docker_sandbox_service.py`（在原 patch 上追加 3 处）** —— sandbox 打用户标记 + 放开上限：
```python
# a) max_num_sandboxes 读 env（os 已 import）
max_num_sandboxes: int = Field(
    default_factory=lambda: int(os.getenv('OH_MAX_NUM_SANDBOXES', '5')), ...)

# b) start_sandbox 接收 user，盖章到 docker label
async def start_sandbox(self, sandbox_spec_id=None, sandbox_id=None,
                        created_by_user_id=None):   # ← 新增参数
    labels = {'sandbox_spec_id': sandbox_spec.id,
              'oh_user_id': created_by_user_id or ''}   # ← 新增 label

# c) _container_to_sandbox_info 读回 label
user_label = (container.labels or {}).get('oh_user_id') or None
return SandboxInfo(id=container.name, created_by_user_id=user_label, ...)
```

**③ `patches/live_status_app_conversation_service.py`（在原 patch 上追加）** —— 传用户给 sandbox + 会话隔离：
```python
# a) _wait_for_sandbox_start 建 sandbox 时带上用户
sandbox = await self.sandbox_service.start_sandbox(
    sandbox_id=sandbox_id_str, created_by_user_id=task.created_by_user_id)

# b) search/count_app_conversations 开头按当前用户过滤
sandbox_id__in = await self._get_current_user_sandbox_ids()
# ...下传 sandbox_id__in

# c) 新增 helper：翻页收集当前用户的所有 sandbox.id（不限 RUNNING）
async def _get_current_user_sandbox_ids(self) -> list[str] | None:
    user_id = await self.user_context.get_user_id()
    if user_id is None: return None   # 单用户回退，不过滤
    ... # search_sandboxes 翻页，过滤 created_by_user_id == user_id，收集 .id

# d) 会话级越权加固（详见 §9.6）：在取数 service 方法里强制归属
async def _filter_owned(self, conversations):   # 不属于当前用户的 → None
    owned = await self._get_current_user_sandbox_ids()
    if owned is None: return conversations      # 单用户回退
    allowed = set(owned)
    return [c if (c and c.sandbox_id in allowed) else None for c in conversations]
# get_app_conversation / batch_get_app_conversations 末尾过 _filter_owned；
# update_app_conversation 取到 info 后直接判 info.sandbox_id 不在集合 → return None
```

**④ `patches/sql_app_conversation_info_service.py`（新，隔离的 SQL 层）** —— `search`/`count`/`_apply_filters` 加 `sandbox_id__in`，空列表短路成"无结果"（避免 `IN ()`）：
```python
from sqlalchemy import (..., literal, ...)   # ← 加 literal
if sandbox_id__in is not None:
    if sandbox_id__in: conditions.append(StoredConversationMetadata.sandbox_id.in_(sandbox_id__in))
    else: conditions.append(literal(False))   # 空集合 → 无结果
```

**⑤ `user_proxy.py`（新，§9.1 的反向代理）** —— `:3000` → app-server `:3010`；`?user=`/`?uid=` 种 cookie `oh_user`，每请求注入头 `X-User-Id`；HTTP + WS 全反代。env：`UP_UPSTREAM`（默认 `http://localhost:3010`）、`UP_LISTEN_PORT`(3000)、`UP_COOKIE_NAME`(oh_user)、`UP_HEADER_NAME`(X-User-Id)。

### 9.4 部署

```bash
# app-server：端口改 3010（内部），加 OH_MAX_NUM_SANDBOXES，多挂两个 patch
docker rm -f openhands-app $(docker ps -aq --filter name=oh-agent-server)
docker run -d --restart unless-stopped \
  -e SANDBOX_USER_ID=1001 -e INIT_GIT_IN_EMPTY_WORKSPACE=1 \
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
  -p 3010:3000 --add-host host.docker.internal:host-gateway \
  --name openhands-app ghcr.io/openhands/openhands:latest

# user_proxy（3000 公网入口）。禁用 pkill -f（同 gateway 教训），用 PID/systemd
UP_UPSTREAM=http://localhost:3010 nohup python3 /home/mocca/openhands/user_proxy.py \
  > /tmp/user_proxy.log 2>&1 & echo $! > /tmp/user_proxy.pid
```

云 SG 保持 **3000 + 8000**（3000 现在是代理入口，3010 不开）。gateway 照旧（§4.3）。

### 9.5 使用

浏览器访问（不同浏览器/隐身窗口用不同 user 即隔离）：
```
http://43.162.100.45:3000/?user=alice
http://43.162.100.45:3000/?user=bob
```
首次访问 `?user=` 在响应里种 cookie `oh_user=<id>`（HttpOnly, Path=/, SameSite=Lax），之后该浏览器所有 API 请求自动带 `X-User-Id`。

### 9.6 会话级越权加固（真隔离，对外必做）

**问题**：只过滤 list/search 只挡住了侧栏。会话级端点仍可被 id 越权——实测 alice 用 bob 的 conversation id 能：按 id 读全文、读文件、改标题、删除。`user_id` 之前**只在建 sandbox / 选复用 / 列表过滤**用到，单条取数不校验归属。

**卡点选择**：所有会话级端点取数最终都走两条路径之一——
- `get_app_conversation(id)` / `batch_get_app_conversations(ids)`：覆盖 按 id 读、批量读、`delete`、以及 `/{id}/file|git|skills|hooks|download`（这些子路由都经 `get_app_conversation` 或共享的 `_get_agent_server_context`）。
- `get_app_conversation_info(id)`（底层）：`update` 走这条，**绕过**上面，需单独加。

所以在 service 层（`live_status_app_conversation_service.py`）两处接入即可全覆盖（代码见 §9.3 ③d）：
- `get_app_conversation` / `batch_get_app_conversations` 末尾过 `_filter_owned()`——不属于当前用户的会话替换成 `None`。
- `update_app_conversation` 取到 `info` 后判 `info.sandbox_id` 不在用户 sandbox 集合 → `return None`（端点转 404）。

**为什么用 `None` 而不是抛异常**：`None` 本就是这些方法的合法返回（端点都已处理：file→`''`、get→404、delete→False）。堵越权不引入新错误路径，最小副作用。`user_id` 为空（单用户/未认证）时 `_filter_owned` 直接放行，保持回退兼容。

**gateway(8000) 不在此范围**：它按 `session_api_key` 路由，是 sandbox 级访问。key 是 per-sandbox 机密、不在会话列表暴露（越权返回的 `[None]` 里 `session_api_key` 也是 null），正常用户拿不到别人的 key。要彻底锁需在 gateway 层校验 key↔user，属更深一层（待办）。

### 9.7 验证（2026-06-17 实测）

| 测试 | 结果 |
|---|---|
| 身份注入 `?user=alice` → start 返回 | `created_by_user_id:"alice"` ✅ |
| alice 开 2 会话 + bob 开 1 会话 | 仅 **2 个 sandbox**（alice 第 2 个**复用**第 1 个；bob 独立）✅ |
| sandbox 容器 label | `oh_user_id=alice` / `oh_user_id=bob` ✅ |
| 会话隔离 | alice 见 2、bob 见 1、无 user 见全部 28 ✅ |
| gateway 路由两 key | `FpO0Ee9B:42129`、`PSLED2f0:47831` 均 200 ✅ |
| 单用户回退（不带 user） | 行为不变，全部会话可见、共用一 sandbox ✅ |
| **越权①** alice 按 id 读 bob 会话 | 加固前返回全文 → 加固后 `[None]` ✅ |
| **越权②** alice 读 bob 会话文件（子路由） | 空内容 ✅ |
| **越权③** alice 改 bob 会话标题（update） | HTTP 404 ✅ |
| **越权⑤** alice 删 bob 会话（delete） | HTTP 404，bob 会话仍在 ✅ |
| 对照：bob 取/改自己会话 | 正常（200/有内容）✅ 不误伤 |
| 对照：不带 user 按 id 取 | 正常有内容 ✅ 单用户回退不误伤 |

验证命令片段：
```bash
# 两个用户各开会话，看 sandbox 数（应 = 用户数，复用不新增）
for u in alice bob; do
  curl -s -X POST http://localhost:3000/api/v1/app-conversations -H 'Content-Type: application/json' -b "oh_user=$u" -d '{}' -o /dev/null
done
docker ps --filter name=oh-agent-server --format '{{.Names}}' | \
  xargs -I{} docker inspect {} --format '{{.Name}} oh_user_id={{.Config.Labels.oh_user_id}}'
# 隔离
for u in alice bob NONE; do
  [ "$u" = NONE ] && b="" || b="-b oh_user=$u"
  n=$(curl -s "http://localhost:3000/api/v1/app-conversations/search?limit=100" $b | python3 -c "import sys,json;print(len(json.load(sys.stdin)['items']))")
  echo "$u -> $n"
done
# 越权加固（§9.6）：alice 拿 bob 的 id 应全部被挡
BOB=$(curl -s "http://localhost:3000/api/v1/app-conversations/search?limit=1" -b 'oh_user=bob' | python3 -c "import sys,json;print(json.load(sys.stdin)['items'][0]['id'])")
curl -s "http://localhost:3000/api/v1/app-conversations?ids=$BOB" -b 'oh_user=alice'   # 期望 [null]
curl -s -o /dev/null -w "update %{http_code}\n" -X PATCH "http://localhost:3000/api/v1/app-conversations/$BOB" -b 'oh_user=alice' -H 'Content-Type: application/json' -d '{"title":"x"}'  # 期望 404
curl -s -o /dev/null -w "delete %{http_code}\n" -X DELETE "http://localhost:3000/api/v1/app-conversations/$BOB" -b 'oh_user=alice'  # 期望 404
```

### 9.8 多用户专属踩坑（追加到 §6）

| 坑 | 现象 | 根因 | 修复 |
|---|---|---|---|
| **sandbox 全打 None，亲和性失效** | alice 连开两会是两个 sandbox（不复用） | `DockerSandboxService` 从不写 `created_by_user_id`（硬编码 None，`start_sandbox` 也不传用户）→ `_find_running_sandbox_for_user` 按 user 过滤永远 0 命中 | 用 docker label `oh_user_id`：建时盖章、`_container_to_sandbox_info` 读回（§9.3 ②） |
| 会话表没 user_id 列 | 想 `created_by_user_id__eq` 过滤会话，发现该字段读出来恒 None | OSS 把 user_id 挪到了不存在的 `ConversationMetadataSaas` | 改按 `sandbox_id__in`（用户的 sandbox 集合）过滤（§9.3 ③④） |
| 3001 端口被占 | app-server 起不来 `bind failed` | 宿主已有个 14 天的 `node server.js` 占着 3001 | 换空闲端口 3010（`UP_UPSTREAM` 跟着改） |
| proxy 连接被 reset | curl :3000 返 `HTTP 000` / Connection reset | `web.run_app(access_log=web.AccessLogger)` 传了**类**而非 logger 实例，每请求访问日志抛错 | 去掉 `access_log=` 参数用默认 |
| `/tmp/gateway.log` 不再刷新 | gateway 像是停了 | 当前 gateway 是被旧 Claude 会话当后台任务起的，stdout 落在 `…/tasks/*.output`，不是 `/tmp/gateway.log` | 真实日志在 `/proc/<pid>/fd/1` 指向的文件；或上 systemd 归位 |
| **list 过滤了但仍能按 id 越权** | alice 拿 bob 的 conversation id 能读/改/删 | 只 patch 了 list/search；单条取数(`get`/`batch_get`/`update`)不校验归属 | service 层取数方法统一卡归属 `_filter_owned` + update 单独判（§9.6） |

### 9.9 多用户已知限制 / 待办

- **会话级越权已堵（§9.6）；gateway(8000) 仍是 sandbox 级**：拿到某 sandbox 的 `session_api_key` 就能直连那个 sandbox。key 不在会话列表暴露、正常用户拿不到，但要彻底锁需在 gateway 校验 key↔user（待办）。
- **隔离靠 sandbox_id 派生**：用户 sandbox 被**删除**（非 pause）则会话变孤儿、列表消失。≤10 用户 / max=15 / 只 pause 不删，风险低。彻底解法 = 给会话表加 `user_id` 列（alembic 迁移），列后续加固。
- **cookie 可伪造**（无签名）：适合内部/可信。需抗伪造可加 HMAC。
- **`default_user_auth.py` 是 LEGACY V0**（注释标 2026-04 移除）。1.8.0 有效；升级 `latest` 要重 diff，文件可能整体被换 → 身份入口届时重接。`docker_sandbox_service.py` / `live_status...` / `sql...` 同理随升级重 diff。
- **无开机自启**：user_proxy（`/tmp/user_proxy.pid`）和 gateway 都是 nohup 跑的，**重启服务器不会自起**。建议 systemd unit（待办）。
- **多用户上限受内存**：3.5G 机器实测 ≤10 用户稳妥；max_num_sandboxes=15 是上限，真到 15 个并发要迁 8G+ 机器。

---

## 9.10 前端登录页（替代 user_proxy.py，2026-06-17 上线）

> 规格 `spec/spec-design-frontend-login-userid.md`，计划 `spec/plan-frontend-login-userid.md`。
> **身份注入从反向代理改为前端**：app-server 直接对外 :3000，去掉 user_proxy 这个有状态单点。

### 架构变化
```
旧：浏览器 ─?user=─► user_proxy(3000) ─X-User-Id─► app-server(3010)
新：浏览器 ─► app-server(3000)  [前端 login.html 登录 → 注入脚本给每个 REST 加 X-User-Id]
```
- `user_proxy.py` 不再需要（已停，文件保留作回滚）。
- gateway(8000)、后端 4 个 py patch、sandbox 打标、会话隔离(§9.6) **全不变**。

### 改动（2 个前端文件，不碰 bundle）
都在 `/home/mocca/openhands/frontend-patch/`，docker 只读挂载：
- **`login.html`** → `/app/frontend/build/login.html`：硬编码 `alice`/`bob`=`123456`，校验通过写 `sessionStorage.oh_user` 跳 `/`。`SPAStaticFiles` 真文件优先，故可直接访问。
- **`index.html`** → `/app/frontend/build/index.html`：`<head>` 最前注入脚本——未登录跳 `/login.html`；patch `fetch`+`XMLHttpRequest`（axios 底层）给**同源** REST 请求加 `X-User-Id`；暴露 `window.ohLogout()`。

### 部署变化（在 §9.4 基础上）
- 端口 `-p 3010:3000` → **`-p 3000:3000`**。
- 先停 user_proxy（`kill $(cat /tmp/user_proxy.pid)`，**禁 pkill -f**）释放 :3000，再起 app-server。
- 追加两挂载：
  ```
  -v /home/mocca/openhands/frontend-patch/login.html:/app/frontend/build/login.html:ro
  -v /home/mocca/openhands/frontend-patch/index.html:/app/frontend/build/index.html:ro
  ```

### 使用
浏览器访问 `http://43.162.100.45:3000/` → 未登录自动跳登录页 → `alice`/`bob` + `123456` 登录。不同浏览器/标签页登不同用户即隔离（`sessionStorage` 按标签隔离）。

### 验证（2026-06-17 实测）
| 测试 | 结果 |
|---|---|
| `/login.html` 可达（真文件非 fallback） | ✅ |
| `index.html` 注入脚本就位、在 axios preload 之前 | ✅（script@1244 < axios@1889）|
| 带 `X-User-Id` 的 REST → 识别用户 | alice/bob count 各自独立 ✅ |
| 新建会话 sandbox 打标 | `oh_user_id='alice'` ✅ |
| app-server 直连 :3000、user_proxy 已停 | ✅ |
| **浏览器实测（2026-06-17）**：`alice`/`bob` + `123456` 均能登录进入主应用 | ✅ 端到端通过 |

### 关键认知（为什么纯前端可行）
- **user_id 只在 REST 用**：app-server 无面向浏览器的 WS；浏览器 WS 直连 gateway:8000 用 `session_api_key`，不要 user_id → 注入脚本无需管 WS（也管不了，WS 握手不能带自定义头）。
- **注入脚本必须在 bundle 之前执行**：放 `<head>` 最前，否则 axios 实例可能已创建并发请求漏头。
- **弱身份，靠后端兜底**：`X-User-Id` 可伪造，但会话级越权已堵（§9.6）→ 伪造只能"变成另一个用户"，不能越权读任意会话。仅限内部可信。

### 回滚
app-server 改回 `-p 3010:3000`、去掉两个 frontend-patch 挂载，重启 `user_proxy.py`（`UP_UPSTREAM=http://localhost:3010 nohup python3 user_proxy.py > /tmp/user_proxy.log 2>&1 &`）即回到 `?user=` cookie 方案。

