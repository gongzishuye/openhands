# 实施计划：源码 fork 自构建镜像（路线 B）——本期只做"复现等价"

> 对应规格：`spec-source-fork-build.md`
> **核心目标（本期，务必锁定）**：从 fork 源码构建出的镜像，行为/内容与**当前运行版本**（官方基线 `96eea1b` + 4 后端 patch + 2 前端 html）**完全一致**，作为后续定制的**受信基线**。先证明"源码能复现现网"，再谈演进。
> **本期范围**：T1~T6（建立受信基线并切换）。**不含**升级演练（spec §9.4）与前端深度定制（spec 后续阶段）——见 §7 后续。
> 模式：fork + 长期分支 `multiuser-deploy`；构建在 CI/大机器；3.5G 生产机只 pull + run。

---

## 0. 前置确认（已核实，无需重做）

- ✅ 现网源码 = 上游 commit `96eea1be4774f841c7685e95042c4b5a2afa43e1`（OCI label `revision` 实测，commit 时间与镜像构建差 18s）。
- ✅ 后端 delta 共 +100/-10，4 文件路径见 spec §4.2，补丁取自基线镜像原文，**必能干净 apply**。
- ✅ 构建 = 上游 `containers/app/Dockerfile`（node 构建前端 → poetry 装后端 → 组装）；改动在源码，Dockerfile 仅需加 1 行前端 overlay（T3）。
- ✅ 基线镜像 digest：`ghcr.io/openhands/openhands@sha256:369c14ab1ad80dd7dd1d18a00dbbe14869070baba7773a864303ff25174b260b`。
- ⚠️ 生产机 3.5G **不可** build 前端（CON-002）→ 构建只在 CI/≥8G 机器。
- ⚠️ **前端构建非确定性**：`react-router build` 产物 bundle 指纹每次可能不同 → `frontend/build` 不要求逐字节等价；等价口径见 §1 的"等价判据"。

---

## 1. 任务分解

| # | 任务 | 产出文件 / 物 | 依赖 |
|---|---|---|---|
| T1 | Fork + 远程 + 分支基线 | `OpenHands-custom` 仓、`multiuser-deploy`@`96eea1b` | — |
| T2 | 后端 delta 抽 4 补丁并入源码 | 4 个 `.patch` + commit "backend: multi-user isolation" | T1 |
| T3 | 前端平价 overlay（构建后覆盖 2 html） | 仓内 `containers/app/overlay/{index,login}.html` + Dockerfile 加 1 行 COPY + commit | T1 |
| T4 | CI/大机器构建 + push 镜像 | `ghcr.io/gongzishuye/openhands-custom:96eea1b-mu1` | T2, T3 |
| T5 | **等价性验证（AC-000 闸门）** | 验证报告（后端 diff = delta；前端源码零差异 + 2 html 一致） | T4 |
| T6 | 生产机切换（去 6 挂载）+ 行为验证 | 新 `docker run`；alice/bob 端到端一致 | T5 |

> **等价判据（AC-000 的可执行口径）**：
> 1. **后端(严格)**：构建镜像 `openhands/` 相对基线镜像，**仅 4 个 py 文件**不同，且 diff == §4.2 的 delta；其余字节相同。
> 2. **前端(源码 + 行为)**：`frontend/` 源码相对上游 `96eea1b` **零差异**；覆盖进 `frontend/build` 的 `index.html`/`login.html` 与现挂载的 `frontend-patch/*` **逐字节一致**；bundle 指纹差异属构建非确定性，**不算违背**。
> 3. **行为**：alice/bob 登录、隔离、各自 sandbox、agent 回话，与现网一致。

---

## 2. 详细步骤

### T1 — Fork、远程、分支基线
```bash
git clone git@github.com:gongzishuye/OpenHands-custom.git
cd OpenHands-custom
git remote add upstream https://github.com/OpenHands/OpenHands.git
git fetch upstream
git checkout -b multiuser-deploy 96eea1be4774f841c7685e95042c4b5a2afa43e1
```
- `main` 跟随上游、不直接提交（CON-004）。校验：`git log -1` HEAD == `96eea1b`。

### T2 — 后端 delta → 补丁 → 并入源码
```bash
# 在补丁仓 /home/mocca/openhands 侧生成 4 个 unified diff（orig_* 已从基线镜像抠出）
for f in default_user_auth docker_sandbox_service \
         sql_app_conversation_info_service live_status_app_conversation_service; do
  diff -u /tmp/.../orig_$f.py patches/$f.py > /tmp/$f.patch || true
done
# 在 OpenHands-custom / multiuser-deploy 上按 §4.2 路径 apply
cd OpenHands-custom
for p in /tmp/*.patch; do git apply --check "$p"; done   # 全部 dry-run 通过再继续
for p in /tmp/*.patch; do git apply "$p"; done
git add -A && git commit -m "backend: multi-user isolation (user_auth/sandbox/conversation)"
```
- ⚠️ 任一 `git apply --check` 报 reject → 基线选错，停下核对 commit。
- 校验：`git diff --stat HEAD~1` 改动量 == §4.2（+9/-4、+16/-4、+16/0、+59/-2）。

### T3 — 前端平价 overlay（保持与现网一致，不改前端源码）
本期**不动前端源码**（保证源码零差异），仅把现有 2 个 html 覆盖进构建产物：
```bash
mkdir -p containers/app/overlay
cp /home/mocca/openhands/frontend-patch/index.html containers/app/overlay/
cp /home/mocca/openhands/frontend-patch/login.html containers/app/overlay/
```
在 `containers/app/Dockerfile` 中，紧跟现有这行之后：
```dockerfile
COPY --chown=openhands:openhands --chmod=770 --from=frontend-builder /app/build ./frontend/build
# ↓ 新增：用现网同款 html 覆盖（平价，与挂载方案字节一致）
COPY --chown=openhands:openhands --chmod=770 ./containers/app/overlay/index.html ./frontend/build/index.html
COPY --chown=openhands:openhands --chmod=770 ./containers/app/overlay/login.html ./frontend/build/login.html
```
```bash
git add -A && git commit -m "frontend: overlay login.html + X-User-Id index.html (parity with current)"
```
> 深度前端定制（改 `frontend/` 源码）是后续阶段（§7），本期刻意不做，以守住"源码零差异"判据。

### T4 — 构建 + push（CI 或 ≥8G 机器，**不在生产机**）
```bash
docker build -f containers/app/Dockerfile \
  -t ghcr.io/gongzishuye/openhands-custom:96eea1b-mu1 \
  --build-arg OPENHANDS_BUILD_VERSION=96eea1b-mu1 .
docker push ghcr.io/gongzishuye/openhands-custom:96eea1b-mu1
```
- 推荐 GitHub Actions：复用/裁剪上游 `.github/workflows` 的 app 镜像构建，触发于 `multiuser-deploy`。

### T5 — 等价性验证（AC-000，本期核心闸门）
在任意有 docker 的机器拉两镜像对比：
```bash
BASE=ghcr.io/openhands/openhands@sha256:369c14ab1ad80dd7dd1d18a00dbbe14869070baba7773a864303ff25174b260b
NEW=ghcr.io/gongzishuye/openhands-custom:96eea1b-mu1
docker pull "$BASE"; docker pull "$NEW"
# 导出两镜像的 openhands/ 源码树并整体 diff
for img in BASE NEW; do
  cid=$(docker create "${!img}"); docker cp "$cid:/app/openhands" "/tmp/oh_$img"; docker rm "$cid">/dev/null
done
diff -rq /tmp/oh_BASE /tmp/oh_NEW       # 期望：仅 4 个 py 文件 differ
# 逐个确认差异 == delta
diff -u /tmp/oh_BASE/app_server/user_auth/default_user_auth.py \
        /tmp/oh_NEW/app_server/user_auth/default_user_auth.py   # 应等于 §4.2 改动
# 前端 2 html 与现挂载逐字节一致
docker run --rm "$NEW" cat /app/frontend/build/login.html | diff - /home/mocca/openhands/frontend-patch/login.html && echo "login OK"
docker run --rm "$NEW" cat /app/frontend/build/index.html | diff - /home/mocca/openhands/frontend-patch/index.html && echo "index OK"
```
- ✅ 通过条件：后端仅 4 文件 differ 且等于 delta；2 html diff 为空。**未通过不进 T6**。
- bundle 指纹差异（`frontend/build/assets/*`）属构建非确定性，**忽略**（§4 风险 R1）。

### T6 — 生产机切换（去掉全部 6 挂载）
```bash
docker pull ghcr.io/gongzishuye/openhands-custom:96eea1b-mu1
docker rm -f openhands-app           # 数据在卷里，安全
# 用 spec §4.5 的启动命令（镜像换成自构建，删 6 个 patch 挂载，其余不变）
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
验证（对应 spec §10 / AC）：
```bash
docker inspect openhands-app --format '{{range .Mounts}}{{.Destination}}{{"\n"}}{{end}}'  # 无 6 个 patch 路径
curl -s http://localhost:3000/login.html | grep -q 'OpenHands 登录' && echo "login OK"
curl -s http://localhost:3000/ | grep -q 'X-User-Id' && echo "inject OK"
```
- 浏览器手验：alice/bob 登录、双标签隔离、各自 sandbox、agent 回话——与现网一致（AC-007）。
- ⚠️ `gateway.py` 不变、仍需在跑（:8000）。

---

## 3. 回滚方案

| 层级 | 操作 |
|---|---|
| 镜像级 | `docker rm -f openhands-app` → 用上一个可用 tag 重启（首次切换无上一个自构建 tag，则走方案级） |
| 方案级（回挂载方案） | `docker rm -f openhands-app` → 执行 `spec-infrastructure-derived-image-frontend.md` §4.4 完整命令（官方镜像 + 6 挂载）。前提：`patches/` 与 `frontend-patch/` 未删（CON-003） |

> 后端 4 patch、`frontend-patch/`、gateway、sandbox 镜像均保留 → 回滚只换镜像 + 加回 6 挂载，秒级。

---

## 4. 风险与注意

- **R1 前端非确定性**：`frontend/build/assets/*` 指纹与官方不同 → **预期**，按 §T5 等价口径忽略；只严判后端字节 + 2 html。
- **R2 前端构建 OOM**：违反 CON-002——必须在 CI/≥8G 机器；**绝不**在 3.5G 生产机 build。
- **R3 补丁 apply reject**：基线非 `96eea1b`；核对 commit 重试。
- **R4 切换窗口**：停旧起新之间 :3000 秒级不可用，可接受（R 同 plan-frontend §R3）。
- **R5 token 泄漏**：推送/CI 凭据走 GitHub Secrets，勿明文入库/聊天；用完即吊销。
- **R6 fork 名冲突**：用 `OpenHands-custom`，勿用 `openhands`（CON-005）。

---

## 5. 文档更新（完成后）

- `README.md` 文档地图：spec 行细分，新增 `spec-source-fork-build.md` + 本 plan。
- `docs/current/architecture.md`：部署章节标注"app-server 改用自构建镜像 `openhands-custom:96eea1b-mu1`，零 patch 挂载；挂载方案留作回滚"。
- 保留挂载方案章节并标"回滚备用"。

---

## 6. 验收清单（Definition of Done，本期=复现等价）

- [ ] T1：`OpenHands-custom` fork 就绪，`multiuser-deploy`@`96eea1b`，`origin`/`upstream` 双远程
- [ ] T2：4 补丁干净 apply，`git diff --stat` == §4.2 delta
- [ ] T3：2 html overlay 进 Dockerfile，前端源码零改动
- [ ] T4：CI/大机器构建成功，镜像 push 且生产机可 pull
- [ ] **T5（核心闸门 AC-000）**：后端仅 4 文件 differ 且 == delta；2 html diff 为空
- [ ] T6：生产机 `docker inspect` 无 6 挂载；`/login.html`、`/` 注入就位
- [ ] T6：alice/bob 登录、隔离、各自 sandbox、agent 回话——与现网一致
- [ ] 文档已更新；挂载方案回滚路径已验证可用

---

## 7. 后续阶段（非本期，受信基线达成后再启）

- **升级演练**：`git rebase --onto <新上游基线> 96eea1b multiuser-deploy` → 重构建 `…-mu2` → 切换验证（spec §9.4）。
- **前端深度定制**：改 `frontend/` 源码（组件/路由层）替代 overlay；遵守 GUD-001"新增文件 + 开关"降 rebase 冲突。届时另起一份 plan。
