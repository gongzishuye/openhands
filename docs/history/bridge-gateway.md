# 多并发 sandbox 验证方案（bridge 模式 + Gateway）

> **状态：⚠️ 已被取代。** 这里的 `/p/{port}` path 路由方案已被推翻，现网改用 session_api_key 路由——以 [`../current/architecture.md`](../current/architecture.md) 为准。本文仅作演进记录保留。

> 目标：在现网 openhands 上快速验证「多个 `oh-agent-server-*` sandbox 并发」，并明确 Gateway 的功能与实现。
> 不依赖 [`../archive/plan-remote-runtime.md`](../archive/plan-remote-runtime.md) 的 remote runtime 方向；现网实际用的是 **app-server + agent-server** 架构。
> 环境：腾讯云 `43.162.100.45`，3.5G / 2 vCPU；`openhands-app`(1.8.0) + `agent-server`(1.27.1)。
> 创建：2026-06-17。

---

## 1. 背景：为什么现网只能跑一个 sandbox

现网用 **host 网络模式**（`AGENT_SERVER_USE_HOST_NETWORK=true`）：每个 sandbox 容器内监听固定端口 `8000/8001/8011/8012`，host 模式下直接占用宿主这些端口。第二个 sandbox 会 `bind 8000 failed`（即 deployment-notes 坑9 的 `address already in use → exit 1`）。

- host 模式当初是为**远程访问**才启用的（坑5：bridge 模式随机端口被云安全组挡），**不是为了限制单 sandbox**。
- 代码 `docker_sandbox_service.py:384` 对 `host 模式 + max>1` 只发 warning，实际第二个 sandbox 必然冲突。

## 2. 关键结论：`max_num_sandboxes` 不用动

- `max_num_sandboxes` 默认 **5**（`docker_sandbox_service.py:599`），测 2 个绰绰有余。
- 唯一障碍是 host 网络。**切 bridge 即可**。

## 3. bridge 模式的多并发原理

- bridge 模式下，每个 sandbox 容器 `-p 随机:8000`，由 `_find_unused_port()`（`docker_sandbox_service.py:107`）分配**不同随机宿主端口**。
- `OH_SANDBOX_CONTAINER_URL_PATTERN` 的 `{port}` 被填成该随机端口（`:204`）。
- N 个 sandbox 各占不同端口，天然并发，互不冲突。

---

## 4. Gateway 的功能（解决远程访问）

bridge 模式 sandbox 用随机端口，**外网浏览器连不上**（云安全组放不开随机端口）。Gateway 就是为解决这个：

| 功能 | 说明 |
|---|---|
| **端口收口** | 监听**一个**固定公网端口（如 8000），N 个 sandbox 共用 |
| **按端口路由** | `http://公网:8000/p/<port>/...` → `localhost:<port>/...`（剥掉 `/p/<port>` 前缀转发到 sandbox 实际端口） |
| **WebSocket 透传** | agent 实时回话走 WS，必须支持 WS upgrade |

**巧思**：把 `OH_SANDBOX_CONTAINER_URL_PATTERN` 设为 `http://公网:8000/p/{port}`，OpenHands 自动生成 `http://公网:8000/p/<随机端口>` 这种 URL。Gateway **完全无状态**，从 URL 里的 port 就能路由，不用维护 sandbox_id → port 映射表。

> 为什么不用 nginx/caddy：它们的 upstream 地址不能从请求 path 动态生成，做不了「路径决定端口」。轻量 Python 反代最干净。

---

## 5. Gateway 实现（Python ~40 行）

```python
# gateway.py — 单端口收口 + 按 /p/{port} 路由 + WS 透传
import re, asyncio
import aiohttp
from aiohttp import web

PATH_RE = re.compile(r'^/p/(\d+)(/.*)?$')

async def proxy_http(request):
    m = PATH_RE.match(request.path)
    if not m:
        return web.Response(status=404)
    port, sub = m.group(1), m.group(2) or '/'
    target = f'http://127.0.0.1:{port}{sub}'
    if request.query_string:
        target += f'?{request.query_string}'
    async with aiohttp.ClientSession() as s:
        async with s.request(request.method, target,
                             headers={k: v for k, v in request.headers.items() if k.lower() != 'host'},
                             data=await request.read()) as r:
            body = await r.read()
            return web.Response(body=body, status=r.status, headers=dict(r.headers))

async def proxy_ws(request):
    m = PATH_RE.match(request.path)
    port, sub = m.group(1), m.group(2) or '/'
    ws_in = web.WebSocketResponse()
    await ws_in.prepare(request)
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f'http://127.0.0.1:{port}{sub}') as ws_out:
            async def pump(a, b):
                async for msg in a:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await b.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await b.send_bytes(msg.data)
            await asyncio.gather(pump(ws_in, ws_out), pump(ws_out, ws_in))
    return ws_in

app = web.Application()
app.router.add_route('GET', '/p/{port}/{tail:.*}', proxy_ws)   # WS 优先匹配
app.router.add_route('*',   '/p/{port}/{tail:.*}', proxy_http)
web.run_app(app, port=8000)
```

运行：`pip install aiohttp && python gateway.py`，云 SG 放行 8000。

> 验证版够跑通。生产版还需补：CORS 头透传、Host 头改写、超时/重连、限流。

---

## 6. Step A — 本机验证多并发（~10 min，不需要 Gateway）

切 bridge + 本机访问，证明「两个 sandbox 能同时跑、不抢端口、workspace 隔离」。

```bash
# 1) 清旧 sandbox（切网络模式前必清，见坑9）+ 停 app
docker rm -f $(docker ps -aq --filter name=oh-agent-server)
docker rm -f openhands-app

# 2) 重建：去掉 AGENT_SERVER_USE_HOST_NETWORK，URL pattern/CORS 改 localhost
docker run -d --restart unless-stopped \
  -e SANDBOX_USER_ID=1001 \
  -e INIT_GIT_IN_EMPTY_WORKSPACE=1 \
  -e SANDBOX_LOCAL_RUNTIME_URL=http://host.docker.internal \
  -e OH_SANDBOX_CONTAINER_URL_PATTERN='http://localhost:{port}' \
  -e OH_PERMITTED_CORS_ORIGINS_0='http://localhost:3000' \
  -e WORKSPACE_MOUNT_PATH=/home/mocca/openhands/workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /home/mocca/openhands/workspace:/opt/workspace_base \
  -v /home/mocca/openhands/.openhands-state:/.openhands \
  -p 3000:3000 \
  --add-host host.docker.internal:host-gateway \
  --name openhands-app ghcr.io/openhands/openhands:latest
```

**验证**（本机浏览器，或 `ssh -L 3000:localhost:3000 user@43.162.100.45` 转发后访问 `localhost:3000`）：
- 开会话 A → `docker ps` 看到 `oh-agent-server-XXX`（占随机端口，如 35065）
- 开会话 B → 看到第二个 `oh-agent-server-YYY`（占另一个随机端口，如 49463）
- 两个**都 running、端口不冲突、各自能跑命令**（A 写文件不影响 B）
- ✅ 证明：bridge 模式原生支持多 sandbox 并发

## 7. Step B — 远程 Gateway 验证（~20 min）

A 通过后，把 pattern 指向 Gateway：

```bash
docker rm -f openhands-app
# 同 Step A 命令，仅改两行：
#   -e OH_SANDBOX_CONTAINER_URL_PATTERN='http://43.162.100.45:8000/p/{port}' \
#   -e OH_PERMITTED_CORS_ORIGINS_0='http://43.162.100.45:3000' \

python gateway.py    # 同机监听 8000
```
云 SG 放行 8000。外网浏览器开两个会话 → 经 `公网:8000/p/<port>` 路由到两个 sandbox。✅ 远程多并发打通。

---

## 8. 回滚（恢复现网单 sandbox host 模式）

现网原配置（已备份）：

```bash
docker rm -f $(docker ps -aq --filter name=oh-agent-server)
docker rm -f openhands-app
docker run -d --restart unless-stopped \
  -e SANDBOX_USER_ID=1001 \
  -e INIT_GIT_IN_EMPTY_WORKSPACE=1 \
  -e AGENT_SERVER_USE_HOST_NETWORK=true \
  -e SANDBOX_LOCAL_RUNTIME_URL=http://host.docker.internal \
  -e OH_SANDBOX_CONTAINER_URL_PATTERN='http://43.162.100.45:{port}' \
  -e OH_PERMITTED_CORS_ORIGINS_0='http://43.162.100.45:3000' \
  -e WORKSPACE_MOUNT_PATH=/home/mocca/openhands/workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /home/mocca/openhands/workspace:/opt/workspace_base \
  -v /home/mocca/openhands/.openhands-state:/.openhands \
  -p 3000:3000 \
  --add-host host.docker.internal:host-gateway \
  --name openhands-app ghcr.io/openhands/openhands:latest
```

## 9. 风险与提醒

- **切 bridge 后远程访问立即断**（坑5 回来）：要么本机/SSH 转发验证（Step A），要么同时上 Gateway（Step B）。
- **本机内存**：2 个 sandbox 空闲约 150M，跑任务时会涨；盯 `docker stats`，OOM 即停。
- **`max_num_sandboxes`**：默认 5 够测；想精确锁 2 个，设 `OH_MAX_NUM_SANDBOXES=2`（验证生效可开 3 会话看第 1 个是否被 pause）。
- **每次切网络模式前必清 `oh-agent-server-*`**（坑9：残留 sandbox 占端口会导致新 sandbox 启动失败 → 401 错位）。
