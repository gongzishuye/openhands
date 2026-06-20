# OpenHands v1.27 本地部署 + 远程访问 经验总结

> **状态：📚 历史参考（基础部署踩坑，多数仍有效）。** 当前架构以 [`../current/architecture.md`](../current/architecture.md) 为准。

> 记录在腾讯云（公网 IP `43.162.100.45`，3.5G 内存 / 60G 盘）上部署 OpenHands、用 Web GUI **远程访问**、接 DeepSeek 的全过程踩坑与解法。最后更新：2026-06-16。

---

## 0. 背景与目标

- 部署 OpenHands（`ghcr.io/openhands/openhands:latest`，即 SDK v1.27 "Agent Canvas" 版），用 Web GUI。
- 浏览器在**远程**（不同公网 IP）访问服务器，不是 localhost 本机访问。
- LLM 用 DeepSeek（`deepseek/deepseek-v4-pro`）。

---

## 1. 最终架构

| 组件 | 容器 | 网络 | 端口 |
|---|---|---|---|
| 前端 + app-server | `openhands-app` | **bridge** | 宿主 `3000` → 容器 `3000` |
| agent 运行时沙箱 | `oh-agent-server-*`（每会话一个） | **host** | 直接占宿主 `8000`(主/WebSocket)、`8001`(VSCode)、`8011/8012` |

持久化（宿主目录）：
- `/home/mocca/openhands/.openhands-state` → 容器 `/.openhands`（SQLite DB + settings.json + 会话）
- `/home/mocca/openhands/workspace` → 容器 `/opt/workspace_base`（工作区）

浏览器访问路径：
- 页面/HTTP API：`http://43.162.100.45:3000`
- 沙箱 WebSocket / 实时事件：`ws://43.162.100.45:8000/sockets/events/...`

---

## 2. 核心经验：踩过的坑与解法

### 坑 1：磁盘 / 内存严重不足（部署前必查）
- **现象**：60G 盘 98% 满（仅剩 1.8G），内存 3.5G + swap 8G 全满。OpenHands 镜像 ~1.3G + 沙箱运行时镜像 ~3.7G，根本拉不下。
- **解法**：清掉不用的 coze 栈（11 容器 + 9 镜像）和 dify 栈（11 容器 + 9 镜像），释放约 14G 磁盘 + ~1G 内存。
- **教训**：部署前先 `df -h` + `free -h` + `docker system df`。OpenHands 镜像 + 沙箱运行时镜像合计要 **5G+**。`/var/lib/docker` 用 `du -sh` 看真实占用（`docker system df` 只算"活跃"的，会低估）。

### 坑 2：镜像名已改名
- 旧的 `ghcr.io/opendevin/openhands` 已失效（OpenDevin → OpenHands 改名）。
- 现 **`ghcr.io/openhands/openhands:latest`**。
- **教训**：用 `docker manifest inspect <image>` 先验镜像是否存在再 pull，别凭记忆。

### 坑 3：SQLite 数据库写不进去（容器崩溃循环）
- **现象**：`sqlite3.OperationalError: unable to open database file` → `Application startup failed` → 容器反复重启。
- **根因**：DB 落在 `/.openhands/openhands.db`，该目录归 `root:root`，而应用进程以 `enduser`(uid 1001) 运行，写不进去。
- **解法**：把 `/.openhands` 挂载到一个由 uid 1001 拥有的宿主目录：
  `-v /home/mocca/openhands/.openhands-state:/.openhands`（顺带持久化 DB 和配置）。
- **排查命令**：`docker exec -u 1001 <c> touch /.openhands/_t` 测写入权限。

### 坑 4：端口 3000 被本机其他应用占用
- **现象**：3000 被自己的 Next.js 应用（`/home/mocca/openhands` 外的 tiger/Chan 项目，`next dev`）占着。我一开始把 OpenHands 映射到 3002 绕开，结果沙箱 webhook 默认回连 `host.docker.internal:3000` → 打到错的应用 → `404`（页面标题是 "Chan Platform"）。
- **解法**：停掉占用 3000 的应用，让 OpenHands 用 3000。**webhook 回调端口必须和 app 实际端口对齐**（由 `OH_SANDBOX_HOST_PORT` 控制，默认 3000）。
- **教训**：改 app 端口不只是改 `-p`，还要同步 webhook 端口配置，否则沙箱回连会打偏。

### 坑 5（最关键、最耗时）：v1.27 远程访问的架构陷阱 —— 沙箱动态端口
- **现象**：前端一直"正在连接...（1-2 分钟）"。浏览器控制台报：
  - `WebSocket connection to 'ws://43.162.100.45:<动态端口>/sockets/events/...' failed`
  - `CORS: 'http://43.162.100.45:3000' 访问 ':<动态端口>' 被拦截`
- **根因（架构层面）**：v1.27 的前端会**直连沙箱容器**建立 WebSocket 和部分 API（不走 app 转发）。bridge 网络模式下，沙箱端口由 `_find_unused_port()` **动态随机分配**（每次会话都变，如 35065/49463/...）。云服务器（腾讯云）的**安全组不可能放行动态端口** → 浏览器外网连不上 → 卡死。
  - 这是 v1.27 给 **localhost 本机访问**设计的，**远程访问天然不兼容**。
- **解法**：开启 **host 网络模式**，让沙箱用**固定端口**（主 `8000`、VSCode `8001`），云安全组只放行 `8000`（+可选 `8001`）。

#### 坑 5 中的大坑：环境变量名搞错
- 控制 host 网络的变量是 **`AGENT_SERVER_USE_HOST_NETWORK=true`**，**不是** `USE_HOST_NETWORK`！
- 代码里写死：`os.getenv('AGENT_SERVER_USE_HOST_NETWORK', '')`（在 `docker_sandbox_service.py` 的 `_get_use_host_network_default`）。
- 我一开始设了 `USE_HOST_NETWORK=true`，**完全不生效**，沙箱还是 bridge（动态端口 49463），白白浪费一轮。
- **教训**：改配置后用 `docker inspect -f '{{.HostConfig.NetworkMode}}' <sandbox>` 验证沙箱真的是 `host` 再继续。

#### 坑 5 的配套配置（缺一不可）
```bash
-e AGENT_SERVER_USE_HOST_NETWORK=true                              # 沙箱用 host 网络（固定端口）
-e OH_SANDBOX_CONTAINER_URL_PATTERN='http://43.162.100.45:{port}'  # 浏览器用公网IP连沙箱
-e OH_PERMITTED_CORS_ORIGINS_0='http://43.162.100.45:3000'         # CORS 放行 app 来源
# 宿主 /etc/hosts 加一行（host网络沙箱靠它回连 app 的 webhook）：
#   127.0.0.1 host.docker.internal
# 腾讯云安全组：放行 TCP 8000（主机 iptables 默认 ACCEPT 不挡，挡的是云 SG）
```
- 注意：主机 iptables（`YJ-FIREWALL-INPUT`）只是个 IP 黑名单（491 条 REJECT 封恶意 IP），**无兜底 DROP**，INPUT 默认 ACCEPT → 主机不挡端口。真正挡外网的是**腾讯云安全组**，必须在云控制台放行 8000。

### 坑 6：litellm 模型名缺 provider 前缀
- **现象**：`litellm.BadRequestError: LLM Provider NOT provided. Pass in the LLM provider... You passed model=deepseek-v4-pro`。
- **根因**：litellm 靠模型名的 provider 前缀决定路由。`deepseek-v4-pro`（无前缀）→ 不知道往哪发。
- **解法**：模型名改成 **`deepseek/deepseek-v4-pro`**。改 `settings.json` 两处：`agent_settings.llm.model` 和 `llm_profiles.profiles.<profile>.model`，改完重启 app（只对新会话生效）。
- **教训**：所有 litellm 模型名都要带前缀（`deepseek/`、`openai/`、`anthropic/`…）。DeepSeek 兜底可用 `deepseek-chat`（2026/7/24 前仍有效，自动映射 V4 flash）。

### 坑 7：host 网络模式只能一个沙箱
- host 网络下沙箱固定占 8000，**多个会话同时开会端口冲突**。一次只用一个会话，开新会话前确保旧沙箱已退出。

### 坑 8：脏状态残留
- 容器多次重建 / 端口改动期间创建的会话，会绑定到已删除的沙箱、webhook 历史混乱，导致前端"已断开/正在连接"卡死且刷新无效。
- **解法**：`docker restart openhands-app` 清内存脏状态 + 新建会话（别复用旧的）。必要时 `docker rm -f` 所有 `oh-agent-server-*` 旧沙箱。

### 坑 9（401 真凶，2026-06-16 定位解决）：host 网络下 app 一重启，session_api_key 就和旧沙箱错位
- **现象**：前端 WebSocket 看着连上了（浏览器日志正常），但 app-server 调沙箱的 **所有 HTTP API 全线 401**：`POST /api/conversations`、`/api/profiles`、`/api/bash/start_bash_command`、`/api/skills`、`/api/hooks`。
- **迷惑点**：浏览器 WS 用自己缓存的 key 能连上沙箱，所以"看起来连上了、实则 app 啥也干不了"——这是坑 7（host 只能一个沙箱）+ 坑 8（脏状态）同时发作的复合症状。
- **根因（日志实锤）**：host 网络模式下沙箱固定占 8000。**app 容器一旦重启**，它内存里「会话 ↔ session_api_key」映射全丢；重启后 app 想新建沙箱重新握手 → 新沙箱启动时 `bind 0.0.0.0:8000 → [Errno 98] address already in use` 直接退出（exit 1）→ app 的 HTTP 客户端拿着**那个刚崩的新沙箱的 key** 去打**还活着的旧沙箱**（key 不同）→ 全线 401。浏览器缓存的是旧沙箱的 key，反而 WS 能连。
- **三条命令定位**：
  - `docker ps -a --filter name=oh-agent-server` 看到**两个**沙箱（一个 `Exited (1)`、一个 `Up`）→ 中招。
  - `docker logs <那个exited沙箱>` 看到 `address already in use` → 确认端口冲突崩掉。
  - app 日志 `401 Unauthorized for .../api/conversations` 且沙箱日志 `HTTPException 401` → 确认 key 错位。
- **解法（标准恢复三连）**：
  ```bash
  docker rm -f $(docker ps -aq --filter name=oh-agent-server)   # 清所有沙箱（含占着 8000 的旧沙箱）
  docker restart openhands-app                                   # 清 app 内存脏状态
  # 浏览器 Ctrl+Shift+R 强刷（清缓存 key）+ 点「新建会话」（别复用旧会话）
  ```
  验证：新建会话后沙箱日志出现 `POST /api/conversations HTTP/1.1" 201`，双侧 401 计数为 0。
- **铁律**：host 网络模式下 **重启 app 前先 `docker rm -f` 所有沙箱**；**一次只开一个会话、不复用旧会话**。

---

## 3. 当前状态与未解决问题（截至写文档时）

- ✅ `openhands-app` 正常（3000，HTTP 200）。
- ✅ 沙箱切到 **host 网络**，监听宿主 8000，本机 `localhost:8000/health → 200`。
- ✅ 模型名修正为 `deepseek/deepseek-v4-pro`。
- ✅ **401 已解决**（2026-06-16 修复并验证）：根因是 host 网络下 **app 一重启就丢 session_api_key 映射**，新建沙箱又因 8000 被旧沙箱占着崩掉（exit 1），app 拿错 key 打旧沙箱 → 全线 401。清沙箱 + 重启 app + 新建会话后，`POST /api/conversations → 201`，沙箱+app 双侧 **0 个 401**，agent 正常回复。详见坑 9。
- ⏳ 待确认：腾讯云安全组 **8000 是否已放行**（决定浏览器外网→8000 能否通）。

---

## 4. 关键命令速查

### 完整启动命令（host 网络远程访问版）
```bash
docker run -d --restart unless-stopped \
  -e SANDBOX_USER_ID=1001 \
  -e WORKSPACE_MOUNT_PATH=/home/mocca/openhands/workspace \
  -e AGENT_SERVER_USE_HOST_NETWORK=true \
  -e OH_SANDBOX_CONTAINER_URL_PATTERN='http://43.162.100.45:{port}' \
  -e OH_PERMITTED_CORS_ORIGINS_0='http://43.162.100.45:3000' \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /home/mocca/openhands/workspace:/opt/workspace_base \
  -v /home/mocca/openhands/.openhands-state:/.openhands \
  -p 3000:3000 \
  --add-host host.docker.internal:host-gateway \
  --name openhands-app \
  ghcr.io/openhands/openhands:latest
```

### 日常运维
```bash
docker logs -f openhands-app                      # app 实时日志
docker logs -f $(docker ps -q --filter name=oh-agent-server | head -1)  # 最新沙箱日志
docker inspect -f '{{.HostConfig.NetworkMode}}' <sandbox>  # 确认沙箱是 host 网络
docker rm -f $(docker ps -aq --filter name=oh-agent-server)  # 清所有沙箱
docker restart openhands-app                      # 清内存脏状态
```

### 诊断三连
```bash
df -h / ; free -h                                  # 资源
sudo ss -ltnp | grep -E ':3000|:8000'              # 端口监听
docker exec -u 1001 openhands-app sh -c 'curl -s -o /dev/null -w "%{http_code}\n" http://host.docker.internal:8000/health'  # app→沙箱 连通性
```

---

## 5. 给后来者的建议

1. **先想清楚访问方式**：v1.27 远程访问是重灾区（沙箱动态端口 + 云防火墙）。如果只是自己用，优先考虑 **SSH 隧道/SOCKS 走 localhost**，或直接用 **OpenHands Cloud**，能省掉整个 host 网络折腾。
2. **要远程跑通**：`AGENT_SERVER_USE_HOST_NETWORK=true` + 固定端口 8000 + 云 SG 放行 8000 + CORS + `/etc/hosts` 加 host.docker.internal，缺一不可。
3. **配置类坑**：环境变量名以**代码里的 `os.getenv` 为准**（如 `AGENT_SERVER_USE_HOST_NETWORK`，不是想当然的 `USE_HOST_NETWORK`）；litellm 模型名必带 provider 前缀。
4. **权限类坑**：容器内非 root 运行时，DB/状态目录必须挂载成对应 uid 可写（uid 用 `SANDBOX_USER_ID=$(id -u)` 对齐宿主用户）。
5. **资源类坑**：部署前清冗余容器/镜像；内存 <4G 别同时跑别的重应用（dify/coze）。
6. **验证习惯**：每改一处配置，立刻用对应命令验证生效（网络模式、端口监听、HTTP 码），别盲推。

---

## 附：主要错误对照表

| 错误 | 含义 | 解法 |
|---|---|---|
| `unable to open database file` | `/.openhands` 归 root，应用写不进 | 挂载 uid 可写的宿主目录到 `/.openhands` |
| 浏览器 `ws://...:动态端口 failed` | 沙箱动态端口被云防火墙挡 | `AGENT_SERVER_USE_HOST_NETWORK=true` 用固定 8000 |
| webhook `404 ... Chan Platform` | 3000 被别的应用占，沙箱回连打偏 | 让 OpenHands 用 3000（停掉占用应用） |
| webhook `404 ... /api/v1/webhooks` | app 端口和沙箱回调端口不一致 | 对齐 `OH_SANDBOX_HOST_PORT` 与实际 app 端口 |
| `LLM Provider NOT provided` | litellm 模型名缺 provider 前缀 | `deepseek-v4-pro` → `deepseek/deepseek-v4-pro` |
| `401 on POST /api/conversations`（app 调沙箱全线 401，但浏览器 WS 正常） | host 网络下 app 重启丢 key 映射，新沙箱因 8000 被旧沙箱占而崩掉，app 拿错 key 打旧沙箱 | `docker rm -f` 所有沙箱 + `docker restart openhands-app` + 浏览器强刷 + 新建会话（坑 9） |
