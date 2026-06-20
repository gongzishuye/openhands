# OpenHands 多用户/多并发部署 —— 索引

> 本目录是腾讯云 `43.162.100.45`（3.5G/2vCPU）上 OpenHands 部署的运维与设计资料 + 实际运行的补丁代码。
> **想了解现状，直接看 [`docs/current/architecture.md`](docs/current/architecture.md)。** 最后更新：2026-06-18。

## 当前架构（一句话）

`openhands-app`(1.8.0) 对外 `:3000`，提供 REST 与（打过补丁的）前端；每个用户/会话独占一个 `oh-agent-server-*`(1.27.1) sandbox 容器；自建 `gateway.py` 在单端口 `:8000` 上按 `session_api_key` 把请求路由到对应 sandbox 的随机 host 端口（HTTP + WebSocket）。身份隔离靠前端登录页注入 `X-User-Id`。

```
浏览器 ──REST(:3000, 注入 X-User-Id)──► openhands-app ──┐
       ──WS/HTTP(:8000, 带 session_api_key)──► gateway.py ──按 key──► oh-agent-server-*（随机 host 端口）
```

## 文档地图

| 文档 | 状态 | 内容 |
|---|---|---|
| [`docs/current/architecture.md`](docs/current/architecture.md) | ✅ **当前权威** | 单端口 gateway + 多 sandbox + 多用户隔离的完整方案、部署、验证、踩坑、回滚 |
| [`docs/history/deployment-notes.md`](docs/history/deployment-notes.md) | 📚 历史参考 | v1.27 单机部署 + 远程访问的基础踩坑（多数仍有效） |
| [`docs/history/bridge-gateway.md`](docs/history/bridge-gateway.md) | ⚠️ 已被取代 | 早期 `/p/{port}` path 路由方案，已被 session_api_key 路由推翻 |
| [`docs/archive/plan-remote-runtime.md`](docs/archive/plan-remote-runtime.md) | 🗄️ 搁置 | 30 人 / 官方 remote-runtime-as-worker 方向，未采用，留作多机扩展参考 |
| [`spec/`](spec/) | 📐 规格 | 前端登录页 + user_id 注入（design + plan）、派生镜像规格 |

## 代码 / 运行件

| 路径 | 作用 | 运行方式 |
|---|---|---|
| `gateway.py` | 单端口 :8000 gateway：session_api_key 路由 + WS 代理 | `nohup python3 gateway.py`，日志 `gateway.log` |
| `patches/docker_sandbox_service.py` | AGENT_SERVER url 固定 8000 + max 可配 | 只读挂载进 `openhands-app` |
| `patches/live_status_app_conversation_service.py` | 会话按 sandbox_id / 用户过滤、越权加固 | 同上 |
| `patches/sql_app_conversation_info_service.py` | 会话信息归属过滤 | 同上 |
| `patches/default_user_auth.py` | 读 `X-User-Id` → 每用户 sandbox 亲和 | 同上 |
| `frontend-patch/index.html` | 注入脚本：给同源请求加 `X-User-Id` | 覆盖前端 build |
| `frontend-patch/login.html` | 登录页（alice/bob = 123456） | 同上 |
| `.openhands-state/` | app-server 状态（sqlite db、settings） | 容器挂载 |

> 注：`user_proxy.py`（旧反向代理身份注入方案）已于 2026-06-18 删除，由 `frontend-patch` 取代。本目录非 git 仓库，删除不可恢复。
