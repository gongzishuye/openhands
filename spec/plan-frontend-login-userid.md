# 实施计划：前端登录页 + 客户端 user_id 注入（替代反向代理）

> 对应规格：`spec-design-frontend-login-userid.md`
> 目标：去掉 `user_proxy.py`，app-server 直接对外 :3000，前端登录后自带 `X-User-Id`。
> 模式：沿用"docker 只读挂载 patch"，不碰 bundle。

---

## 0. 前置确认（已核实，无需重做）

- ✅ `SPAStaticFiles` 真文件优先 → `/login.html` 可被正常 serve。
- ✅ user_id 只在 REST 用，WS 直连 gateway:8000 不需要 → 纯前端无缺口。
- ✅ axios 底层走 XHR；注入脚本 patch `fetch` + `XMLHttpRequest`。
- ✅ 静态目录 `/app/frontend/build`，`index.html` 可挂载覆盖。
- ✅ 后端会话级越权已堵（§9.6），前端弱身份可接受（内部可信）。

---

## 1. 任务分解

| # | 任务 | 产出文件 | 依赖 |
|---|---|---|---|
| T1 | 写登录页 | `/home/mocca/openhands/frontend-patch/login.html` | — |
| T2 | 写注入脚本 + 改 index.html | `/home/mocca/openhands/frontend-patch/index.html` | 需先 `docker cp` 原 index.html |
| T3 | 重启 app-server：端口 3010→3000，挂载两文件 | （部署命令） | T1, T2 |
| T4 | 停掉 user_proxy.py | （进程管理） | T3 |
| T5 | 端到端验证 | （curl + 浏览器） | T3, T4 |

> 约定：新文件统一放 `/home/mocca/openhands/frontend-patch/`（与 `patches/` 区分：那是后端 py patch，这是前端静态文件）。

---

## 2. 详细步骤

### T1 — login.html
- 内容见规格 §9.2（硬编码 `alice`/`bob` = `123456`，校验通过写 `sessionStorage.oh_user` → 跳 `/`）。
- 单文件内联 CSS，无外部依赖。

### T2 — index.html（注入脚本）
```bash
mkdir -p /home/mocca/openhands/frontend-patch
docker cp openhands-app:/app/frontend/build/index.html \
  /home/mocca/openhands/frontend-patch/index.html
# 在 <head> 最前（所有 modulepreload/script 之前）插入规格 §9.1 的注入脚本
```
- **关键**：脚本必须在 `<head>` 第一个元素，早于 bundle 任何 JS（CON-003），否则 axios 实例可能已创建。
- 脚本职责：未登录跳 `/login.html`（登录页豁免）；同源请求注入 `X-User-Id`；暴露 `window.ohLogout`。

### T3 — 重启 app-server（端口改 3000 + 两个新挂载）
在现有 `docker run`（§../docs/current/architecture.md §9.4）基础上改两处：
- 端口 `-p 3010:3000` → **`-p 3000:3000`**。
- 追加挂载：
  ```
  -v /home/mocca/openhands/frontend-patch/login.html:/app/frontend/build/login.html:ro
  -v /home/mocca/openhands/frontend-patch/index.html:/app/frontend/build/index.html:ro
  ```
- 其余 env / 后端 patch 挂载保持不变（`OH_MAX_NUM_SANDBOXES=15`、4 个 .py patch 等）。
- ⚠️ 注意 :3000 此前被 user_proxy 占用，必须先做 T4 或同步释放。

### T4 — 停 user_proxy.py
- 用 PID 停：`kill $(cat /tmp/user_proxy.pid)`（**禁用 `pkill -f`**，会误杀 bash）。
- 确认 :3000 已释放后再起 app-server（T3 与 T4 顺序：先停 proxy → 再起 app-server 到 3000）。
- `user_proxy.py` 文件保留（回滚用），仅停进程。

### T5 — 验证（对应规格 §10 / §5 AC）
```bash
# 1. 登录页可达（真文件，非 index）
curl -s http://localhost:3000/login.html | grep -q 'OpenHands 登录' && echo "login.html OK"
# 2. index 注入脚本就位
curl -s http://localhost:3000/ | grep -q 'X-User-Id' && echo "inject script OK"
# 3. 模拟前端带头请求 → 隔离生效
for u in alice bob; do
  n=$(curl -s -H "X-User-Id: $u" "http://localhost:3000/api/v1/app-conversations/count")
  echo "$u count -> $n"
done
# 4. 浏览器：未登录访问 / → 跳 login；alice 登录 → F12 看 /api 带 X-User-Id；agent 回话
```
浏览器手验：AC-001（跳转）、AC-004（头注入）、AC-006（双标签页隔离）、AC-008（agent 回话）。

---

## 3. 回滚方案

| 步骤 | 操作 |
|---|---|
| 1 | app-server 改回 `-p 3010:3000`，去掉两个 frontend-patch 挂载 |
| 2 | 重启 `user_proxy.py`：`UP_UPSTREAM=http://localhost:3010 nohup python3 user_proxy.py > /tmp/user_proxy.log 2>&1 &` |
| 3 | 回到 `?user=alice` cookie 方案 |

> 后端 4 个 py patch、gateway、sandbox 打标均不受影响，回滚只动前端层与端口。

---

## 4. 风险与注意

- **R1 注入时机**：脚本没放够靠前 → axios 已发请求漏头。缓解：放 `<head>` 第一个，验证 F12 首个 `/api` 请求即带头。
- **R2 升级 latest**：bundle 文件名变不影响（不碰 bundle）；但 `index.html` 结构若变，注入位置要重对。`login.html` 不受影响。
- **R3 端口切换窗口**：停 proxy 到起 app-server 之间 :3000 短暂不可用（秒级），可接受。
- **R4 弱身份**：`X-User-Id` 可伪造，靠后端 §9.6 兜底；对外不可信场景不可用。

---

## 5. 文档更新（完成后）

- 更新 `../docs/current/architecture.md` §9：标注 `user_proxy.py` 已被前端登录方案替代（§9.1 架构图、§9.4 部署、§9.9 待办）。
- 保留 user_proxy 章节并标"已弃用/回滚备用"。

---

## 6. 验收清单（Definition of Done）

- [ ] `login.html` 部署，`/login.html` 返回登录页
- [ ] `index.html` 注入脚本就位，未登录跳登录页
- [ ] `alice`/`bob` + `123456` 能登录，错误密码被拒
- [ ] `/api/*` 请求带 `X-User-Id`（F12 可见）
- [ ] alice/bob 会话隔离、各自独立 sandbox
- [ ] `user_proxy.py` 已停，app-server 直连 :3000
- [ ] agent 能正常回话（WS 经 gateway）
- [ ] 文档已更新
