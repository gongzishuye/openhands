---
title: 前端登录页 + 客户端 user_id 注入（替代反向代理）
version: 1.0
date_created: 2026-06-17
last_updated: 2026-06-17
owner: shiyaqi (openhands 多用户)
tags: [design, frontend, auth, multi-user, openhands]
---

# Introduction

为 OpenHands 多用户部署增加一个**纯前端登录页**，初始化两个用户（`alice` / `bob`，密码 `123456`），登录后把 `user_id` 保存在浏览器，并由前端在每个发往 app-server 的 REST 请求自动注入 `X-User-Id` 头。**目标是去掉现有的 `user_proxy.py` 反向代理**——身份注入责任从代理转移到前端。

本规格基于对运行中容器（`openhands-app`, OpenHands 1.8.0 + agent-server 1.27.1）的代码核实，所有关键假设均已验证（见 §7）。

## 1. Purpose & Scope

**目的**：用一个最小改动的前端登录方案，替代 §../docs/current/architecture.md §9.1 中的 `user_proxy.py(:3000)`，使 app-server 可直接对外（`:3000`），前端自行携带 `X-User-Id`。

**范围内**：
- 新增静态登录页 `login.html`。
- 注入脚本：拦截浏览器 HTTP 客户端（`fetch` + `XMLHttpRequest`），为同源 `/api/*`、`/config*`、`/mcp/*` 等 app-server 请求注入 `X-User-Id`。
- 未登录时强制跳转登录页；登出能力。
- 部署调整：app-server 端口 `3010 → 3000`、移除 `user_proxy.py`。

**范围外**：
- 真正的鉴权/密码安全（本方案密码前端可见，仅适用于**内部可信用户**）。
- gateway(`:8000`) 的改动（保持不变，按 `session_api_key` 路由）。
- 会话级越权加固（已在后端完成，见 §../docs/current/architecture.md §9.6，不受本变更影响）。
- 前端 bundle 源码修改 / 重新构建。

**目标读者**：实现该功能的工程师 / AI 编码助手。

## 2. Definitions

| 术语 | 定义 |
|---|---|
| **app-server** | OpenHands 主服务（容器 `openhands-app`），提供 Web GUI 静态资源 + REST API，内部监听容器 3000 端口。 |
| **user_proxy.py** | 当前在宿主 :3000 运行的 aiohttp 反向代理，把 `?user=` 转 cookie 并注入 `X-User-Id`。**本方案将移除它。** |
| **gateway** | 宿主 :8000 的 sandbox 路由器，按 `session_api_key` 转发到各 sandbox。**本方案不改动。** |
| **X-User-Id** | app-server 识别用户的 HTTP 请求头。patched `DefaultUserAuth.get_instance()` 读取它（见 §../docs/current/architecture.md §9.3①）。 |
| **bundle** | `/app/frontend/build/assets/*.js`，已编译压缩的 React 前端，**无源码**。 |
| **SPAStaticFiles** | app-server 的静态文件处理类（`/app/openhands/app_server/static.py`）：真实文件存在则返回真文件，否则 fallback 到 `index.html`。 |
| **sessionStorage** | 浏览器按标签页隔离的存储；关闭标签页即清空。本方案用它存 `user_id`。 |

## 3. Requirements, Constraints & Guidelines

### 功能需求
- **REQ-001**：提供静态登录页 `/login.html`，含用户名、密码输入与登录按钮。
- **REQ-002**：初始化两个用户，硬编码于登录页 JS：`alice`/`123456`、`bob`/`123456`。
- **REQ-003**：用户名+密码校验通过后，将 `user_id`（即用户名）写入 `sessionStorage`，键名 `oh_user`，随后跳转到 `/`。
- **REQ-004**：校验失败时在页面显示错误提示，不跳转、不写入存储。
- **REQ-005**：主应用页（`index.html`）加载时，若 `sessionStorage.oh_user` 为空，立即重定向到 `/login.html`。
- **REQ-006**：注入脚本必须拦截 `window.fetch` 与 `XMLHttpRequest`，对所有发往 app-server 的同源相对请求注入头 `X-User-Id: <oh_user>`。
- **REQ-007**：提供登出能力——清除 `sessionStorage.oh_user` 并跳回 `/login.html`。
- **REQ-008**：登录页本身（`/login.html`）及其静态资源请求**不得**被重定向逻辑拦截（避免循环跳转）。

### 安全需求
- **SEC-001**：本方案为**弱身份**：密码硬编码在前端、`X-User-Id` 可被用户手工伪造。仅适用于内部可信用户，安全级别与原 cookie 方案一致（见 §../docs/current/architecture.md §9.9）。
- **SEC-002**：会话级数据越权由**后端**保证（§9.6 已实现），不依赖前端身份的真实性。即：即使用户伪造 `X-User-Id`，也只能访问"伪造成的那个用户"的数据，无法绕过后端归属校验读取任意会话——前端身份只决定"我是谁"，不决定"我能不能越权"。
- **SEC-003**：注入脚本只对**同源**请求注入 `X-User-Id`，禁止向跨域请求注入（防止 user_id 泄露到第三方）。

### 约束
- **CON-001**：禁止修改前端 bundle（`assets/*.js`）——升级 `openhands:latest` 时 bundle 文件名与内容会变。所有改动限定在新增 `login.html` 与覆盖 `index.html`。
- **CON-002**：`index.html` 与 `login.html` 通过 docker `-v` 只读挂载注入，不直接改容器内文件（与现有 patch 模式一致）。
- **CON-003**：注入脚本必须在 bundle 的任何 JS 执行**之前**运行（即放在 `index.html` `<head>` 最前、所有 `modulepreload`/`script` 之前），否则 axios 实例可能在 patch 前已创建并发出请求。
- **CON-004**：app-server 的 WebSocket 路径不经过 user_id（WS 直连 gateway:8000 按 `session_api_key` 路由），故注入脚本**无需**处理 WS；浏览器也无法为 WS 握手设置自定义头。
- **CON-005**：去掉代理后 app-server 直接对外，端口由 `3010` 改为 `3000`；云安全组保持放行 `3000 + 8000`。

### 指南
- **GUD-001**：`user_id` 用 `sessionStorage` 而非 `localStorage`——不同标签页可登录不同用户，便于测试隔离；关闭即登出。
- **GUD-002**：注入判断用"相对 URL 或同源绝对 URL"作为同源依据，避免硬编码主机名（兼容 IP 变更）。
- **GUD-003**：登录页样式从简，单文件内联 CSS，无外部依赖。

### 模式
- **PAT-001**：沿用现有"docker 只读挂载 patch"部署模式（见 §../docs/current/architecture.md §9.4）。

## 4. Interfaces & Data Contracts

### 4.1 存储契约

| 键 | 存储 | 值 | 写入方 | 读取方 |
|---|---|---|---|---|
| `oh_user` | `sessionStorage` | 用户名字符串（`alice`/`bob`） | `login.html` 登录成功 | `index.html` 注入脚本（重定向判断 + 头注入） |

### 4.2 HTTP 头契约

| 头 | 值 | 注入条件 |
|---|---|---|
| `X-User-Id` | `sessionStorage.oh_user` 的值 | 同源、且 `oh_user` 非空 |

app-server 侧读取逻辑（已存在，patched）：
```python
# /app/openhands/app_server/user_auth/default_user_auth.py
uid = request.headers.get('X-User-Id')
return DefaultUserAuth(_user_id=uid)
```

### 4.3 文件部署契约

| 文件 | 容器内路径 | 挂载方式 |
|---|---|---|
| `login.html` | `/app/frontend/build/login.html` | `-v .../login.html:/app/frontend/build/login.html:ro` |
| `index.html`（含注入脚本） | `/app/frontend/build/index.html` | `-v .../index.html:/app/frontend/build/index.html:ro` |

### 4.4 路由行为（由 SPAStaticFiles 决定，已核实）

| 请求路径 | 行为 |
|---|---|
| `GET /login.html` | 真实文件存在 → 直接返回 `login.html`（不 fallback） |
| `GET /` | 返回 `index.html`（含注入脚本） |
| `GET /some/spa/route` | 真实文件不存在 → fallback 返回 `index.html` |
| `GET /api/...` | 命中 API 路由（不经 SPAStaticFiles） |

## 5. Acceptance Criteria

- **AC-001**：Given 未登录（`sessionStorage.oh_user` 为空），When 访问 `http://<host>:3000/`，Then 浏览器重定向到 `/login.html`。
- **AC-002**：Given 在登录页输入 `alice`/`123456`，When 点击登录，Then `sessionStorage.oh_user === "alice"` 且跳转到 `/`。
- **AC-003**：Given 输入错误密码（如 `alice`/`000000`），When 点击登录，Then 显示错误提示，不跳转，`sessionStorage.oh_user` 仍为空。
- **AC-004**：Given 已登录为 `alice`，When 前端发出任意 `/api/*` REST 请求，Then 该请求头包含 `X-User-Id: alice`（F12 Network 可见）。
- **AC-005**：Given 已登录为 `alice`，When 加载会话列表，Then 只显示 alice 的会话（后端按 user_id 过滤，端到端验证 user_id 注入生效）。
- **AC-006**：Given 两个浏览器标签页分别登录 `alice` 与 `bob`，When 各自开会话，Then 互不可见、各自命中独立 sandbox。
- **AC-007**：Given 已登录，When 触发登出，Then `sessionStorage.oh_user` 被清除并跳回 `/login.html`。
- **AC-008**：The system shall 在去掉 `user_proxy.py` 后仍正常工作——app-server 直接监听 :3000，agent 能正常回话（WS 经 gateway:8000）。
- **AC-009**：Given 访问 `/login.html`，When 页面加载，Then 不会因重定向逻辑产生循环跳转。

## 6. Test Automation Strategy

- **Test Levels**：手动端到端为主（前端无构建管线）；可选 curl 脚本验证头注入与隔离。
- **Frameworks**：无（纯静态 HTML+JS）；验证用 `curl` + 浏览器 F12。
- **Test Data Management**：测试用户 `alice`/`bob` 即测试数据；测试后用 API delete 会话 + `docker rm` sandbox 清理（见 §../docs/current/architecture.md §9.7 清理流程）。
- **CI/CD Integration**：不适用（手工部署）。
- **Coverage Requirements**：覆盖 §5 全部 AC。
- **Performance Testing**：不适用（登录页为静态，注入脚本为常量开销）。

## 7. Rationale & Context

### 为什么纯前端方案可行（关键核实结论）
1. **user_id 只在 REST 用**：核实 app-server 无面向浏览器的 WS 端点；浏览器 WS 直连 gateway:8000 用 `session_api_key`，不需要 user_id。故前端只需处理 REST（CON-004）。
2. **axios 底层走 XHR**：bundle `open-hands-axios-*.js` 含 `XMLHttpRequest`，无 `fetch(`。但为稳健起见注入脚本同时 patch `fetch` 与 `XMLHttpRequest`（REQ-006）。
3. **静态文件真文件优先**：`SPAStaticFiles.get_response` 先尝试真实文件，存在即返回——所以新增 `/login.html` 能被正常 serve，不会被 SPA fallback 吞掉（§4.4 核实）。
4. **index.html 可注入**：`index.html` 是普通静态文件，可用只读挂载覆盖；脚本放 `<head>` 最前即可在 bundle 之前执行（CON-003）。

### 为什么把身份注入从代理移到前端
- 去掉 `user_proxy.py` 减少一个有状态单点（原代理挂掉则全员断，见 §9.9）。
- 前端持有 user_id 后，`?user=` URL 参数与 cookie 机制不再需要，登录语义更清晰。

### 为什么这是"弱身份"且可接受
- 后端会话级越权已在 §9.6 堵住：伪造 `X-User-Id` 只能"变成另一个用户"，无法越权读取任意会话。前端身份只回答"我是谁"，真正的数据归属由后端 sandbox 归属校验保证（SEC-002）。

## 8. Dependencies & External Integrations

### External Systems
- **EXT-001**：OpenHands app-server（容器 `openhands-app`）—— 提供静态资源与 REST API，读取 `X-User-Id`。

### Infrastructure Dependencies
- **INF-001**：Docker —— 通过只读挂载注入 `login.html` / `index.html`。
- **INF-002**：gateway(`:8000`) —— sandbox 路由，保持运行不变。

### Data Dependencies
- 无外部数据源；用户清单硬编码于登录页。

### Technology Platform Dependencies
- **PLT-001**：现代浏览器，支持 `sessionStorage`、`fetch`、`XMLHttpRequest` 拦截（ES2017+）。

### Compliance Dependencies
- 无（内部可信场景，非合规系统）。

## 9. Examples & Edge Cases

### 9.1 注入脚本（放 index.html `<head>` 最前，CON-003）

```html
<script>
(function () {
  var USER = sessionStorage.getItem('oh_user');
  // REQ-005 / AC-001：未登录跳登录页（登录页自身豁免，REQ-008/AC-009）
  if (!USER && location.pathname !== '/login.html') {
    location.replace('/login.html');
    return;
  }
  if (!USER) return;

  // SEC-003：仅同源注入
  function sameOrigin(url) {
    try {
      var u = new URL(url, location.href);
      return u.origin === location.origin;
    } catch (e) { return false; }
  }

  // REQ-006：patch fetch
  var _fetch = window.fetch;
  window.fetch = function (input, init) {
    init = init || {};
    var url = (typeof input === 'string') ? input : (input && input.url) || '';
    if (sameOrigin(url)) {
      var h = new Headers(init.headers || (typeof input !== 'string' && input.headers) || {});
      h.set('X-User-Id', USER);
      init.headers = h;
    }
    return _fetch.call(this, input, init);
  };

  // REQ-006：patch XMLHttpRequest（axios 浏览器端底层）
  var _open = XMLHttpRequest.prototype.open;
  var _send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, url) {
    this.__oh_same = sameOrigin(url);
    return _open.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    if (this.__oh_same) {
      try { this.setRequestHeader('X-User-Id', USER); } catch (e) {}
    }
    return _send.apply(this, arguments);
  };

  // REQ-007：暴露登出
  window.ohLogout = function () {
    sessionStorage.removeItem('oh_user');
    location.replace('/login.html');
  };
})();
</script>
```

### 9.2 登录页 login.html（REQ-001~004，硬编码校验）

```html
<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>登录 - OpenHands</title>
<style>
  body{font-family:sans-serif;display:flex;height:100vh;margin:0;align-items:center;justify-content:center;background:#0f1117;color:#e6e6e6}
  .box{background:#1a1d27;padding:32px;border-radius:12px;width:300px}
  input{width:100%;box-sizing:border-box;margin:8px 0;padding:10px;border-radius:6px;border:1px solid #333;background:#11131a;color:#fff}
  button{width:100%;padding:10px;margin-top:12px;border:0;border-radius:6px;background:#4f7cff;color:#fff;cursor:pointer}
  .err{color:#ff6b6b;min-height:18px;font-size:13px;margin-top:8px}
</style></head><body>
<div class="box">
  <h2>OpenHands 登录</h2>
  <input id="u" placeholder="用户名 (alice / bob)" autocomplete="username"/>
  <input id="p" type="password" placeholder="密码" autocomplete="current-password"/>
  <button id="go">登录</button>
  <div class="err" id="err"></div>
</div>
<script>
  var USERS = { alice: '123456', bob: '123456' };   // REQ-002，内部可信场景
  function login() {
    var u = document.getElementById('u').value.trim();
    var p = document.getElementById('p').value;
    if (USERS[u] && USERS[u] === p) {               // REQ-003
      sessionStorage.setItem('oh_user', u);
      location.replace('/');
    } else {                                         // REQ-004
      document.getElementById('err').textContent = '用户名或密码错误';
    }
  }
  document.getElementById('go').onclick = login;
  document.getElementById('p').addEventListener('keydown', function(e){ if(e.key==='Enter') login(); });
</script>
</body></html>
```

### 9.3 边界情况

| 情况 | 预期处理 |
|---|---|
| 直接访问 `/login.html` 且已登录 | 允许停留（可在登录页加"已登录则跳 /"，可选）；不得循环跳转（AC-009） |
| `sessionStorage` 被禁用 | 登录后无法持久 → 每次跳登录页；属浏览器限制，提示用户开启 |
| 同一浏览器两标签页不同用户 | 各标签 `sessionStorage` 独立，互不干扰（GUD-001） |
| 用户手工改 `sessionStorage.oh_user` 伪造身份 | 前端放行，但后端归属校验兜底（SEC-002）——只能访问伪造成的那个用户的数据 |
| 跨域请求（如外部 CDN） | 不注入 `X-User-Id`（SEC-003） |
| bundle 在注入脚本前发请求 | 通过脚本置于 `<head>` 最前避免（CON-003） |

## 10. Validation Criteria

1. `curl http://<host>:3000/login.html` 返回登录页 HTML（HTTP 200，非 index 内容）。
2. `curl http://<host>:3000/` 返回的 `index.html` `<head>` 最前包含注入脚本。
3. 浏览器未登录访问 `/` → 跳 `/login.html`（AC-001）。
4. `alice`/`123456` 登录 → F12 Network 中 `/api/*` 请求头含 `X-User-Id: alice`（AC-004）。
5. alice 会话列表只见 alice 的会话；与 bob 隔离（AC-005/006）。
6. 移除 `user_proxy.py`、app-server 改 :3000 后，agent 正常回话（AC-008）。
7. 登出清除存储并跳登录页（AC-007）。

## 11. Related Specifications / Further Reading

- `/home/mocca/openhands/docs/current/architecture.md` §9 —— 多用户方案全貌（身份 patch、sandbox 打标、会话隔离 §9.6、部署 §9.4）。本规格替换其中 §9.1 的 `user_proxy.py` 环节。
- `/app/openhands/app_server/user_auth/default_user_auth.py`（patched）—— `X-User-Id` 读取点。
- `/app/openhands/app_server/static.py` —— `SPAStaticFiles` 真文件优先逻辑。
- `/app/openhands/app_server/app.py:76-78` —— 静态目录 `./frontend/build` 挂载点。
