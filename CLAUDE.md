# CLAUDE.md

OpenHands 多用户/多并发部署的运维资料 + 实际运行的补丁代码。
**本目录不是 git 仓库,删除/覆盖不可恢复——动文件前先确认。**

## 先读这个
当前架构以 `docs/current/architecture.md` 为准。文档地图见 `README.md`。
`docs/history/` 和 `docs/archive/` 是历史/搁置方案,排查现网问题时别参照。

## 运行拓扑(一句话)
浏览器 → `openhands-app`(:3000, REST + 前端,注入 X-User-Id)
       → `gateway.py`(:8000, 按 session_api_key 路由 HTTP/WS) → `oh-agent-server-*` sandbox(随机 host 端口)

## 红线 / 易踩的坑
- **别移动 `gateway.py` / `patches/` / `frontend-patch/`**:被运行进程和容器挂载按绝对路径引用,移动即坏。
- **重启 gateway 用 PID,禁止 `pkill -f gateway.py`**(会连发起命令的 bash 一起杀)。
  重启:`kill <pid>` 后 `nohup python3 /home/mocca/openhands/gateway.py > gateway.log 2>&1 &`
- **前端会开两条 WS**(主会话 + 父/plan 子会话)。任一条断都会弹 "Failed to connect to server",
  但功能可能正常——优先查 `gateway.log` 的 `[ws]` 错误,而不是当作功能故障。
- 改 `gateway.py` / `patches/` 后必须重启对应进程/容器才生效。

## 状态(2026-06-18)
- gateway WS 代理已修复(`handle_ws` 改 FIRST_COMPLETED + heartbeat),ws 错误归零。
- `user_proxy.py` 已删除,身份注入改由 `frontend-patch` 前端登录页完成。
- 待办:app-server `POST /api/v1/webhooks/conversations` 偶发 500(conversation_id UNIQUE 冲突),未处理。
