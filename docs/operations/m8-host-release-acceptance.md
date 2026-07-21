# M8 宿主机发布验收

本文是 M8 完整发布验收的 operator runbook。唯一可封存入口是仓库根目录的
`make release-acceptance`。它认证的范围仅限：全新 PostgreSQL 数据库、仓库提供的
`make setup-db` / `make start` 宿主机路径、桌面版 Chromium，以及
`deepseek-v4-pro`。

## 当前认证状态

M8 已完成，项目优先、多用户 SaaS V1 总体进度为 8/8（100%）。关闭前实现提交
`896fe62ec4265a343ab6a6d209453d11508d81a0` 已通过 fresh candidate、完整分支 0/0/0 审查和
fresh final；固定 stage manifest digest 为
`dcda2974d83e9c3ed336e8099e2fc74b219f8fb000044c47a1430327fabe8312`。该记录不替代后续版本提交
自身的 candidate/review/final，也不表示已经创建 tag、推送远端或发布制品。

M8 验收始终使用本次 invocation 创建的随机 `deerflow_test_*` 数据库和独立
`deerflow_restore_*` 恢复库。`DATABASE_URL` 只提供普通应用角色和连接模板，
`POSTGRES_ADMIN_URL` 只提供创建、验证和删除这些受控数据库的维护权限。不要把业务库
作为验收目标，也不要手工创建测试库、安装数据库或安装第三方中间件。

## 前置条件

- Git 位于非 detached 的 clean commit，完整审查基线 `3f574b89` 可读取。
- PostgreSQL 维护账号能够创建数据库；应用角色可登录且不是 superuser，也没有
  `CREATEDB`、`CREATEROLE`、replication 或 bypass-RLS 权限。
- `config.yaml` 是当前版本，且恰好配置一个 provider model ID 为
  `deepseek-v4-pro` 的 DeepSeek 模型。
- Python、uv、Node.js、pnpm、PostgreSQL client、Nginx 和 Playwright Chromium 已按
  仓库安装流程准备；端口 `2026`、`3000`、`8001` 空闲。
- 恢复密钥、审计 keyring 和 JWT secret 相互独立，并由 operator 的安全存储提供。

先运行只读检查：

```bash
make doctor
make check-db
```

敏感值不要直接写入命令行、脚本、`.env`、Git 文件或 shell history。可在当前终端通过
隐藏输入读取，再导出所需变量；变量名可以记录，变量值不得进入证据或日志：

```bash
read -rs DEEPSEEK_API_KEY && export DEEPSEEK_API_KEY
read -rs POSTGRES_ADMIN_URL && export POSTGRES_ADMIN_URL
read -rs DATABASE_URL && export DATABASE_URL
```

其余恢复、审计和认证变量使用相同方式从 operator secret store 注入。完成验收后从当前
shell 取消导出；不要把值复制到 runbook 或 issue。

## 第一次 fresh candidate

```bash
M8_LIVE_ACCEPTANCE=1 make release-acceptance
```

成功结果必须是 `candidate_ready`，不是发布完成。命令输出包含不敏感的
`evidence_relative_locator`，例如指向本地 `.release-evidence/<acceptance_run_id>/manifest.json`
的仓库相对定位符。将它解析为 operator-local 变量，不要提交该目录：

```bash
M8_CANDIDATE_MANIFEST="$PWD/<candidate evidence_relative_locator>"
M8_REVIEW_REPORT_PATH="${M8_CANDIDATE_MANIFEST%/manifest.json}/review.json"
```

候选 manifest 必须显示全部固定阶段成功、专用 `postgres.m1_m8` 阶段 skip 为 0、恢复和
回切成功，以及 cleanup 的 process、port、database、path residual 都为 0。后端全量测试中
由 live/PostgreSQL/平台条件保护且已由专用阶段覆盖的 expected skip 可以非零，但必须保留在
该阶段的计数证据中；任一失败都保持 M8 未完成。

## 独立审查与 closed report

审查者必须检查完整 `3f574b89..HEAD`，并独立给出 Critical、Important、Minor 数量。
验收 runner 不替审查者决定 finding，也不能把非零 finding 转换为通过。完成审查后运行：

```bash
cd backend
uv run python scripts/create_m8_review_report.py \
  --candidate-manifest "$M8_CANDIDATE_MANIFEST" \
  --review-base 3f574b89 \
  --review-range 3f574b89..HEAD \
  --critical 0 --important 0 --minor 0 \
  --output "$M8_REVIEW_REPORT_PATH"
cd ..
```

工具只写一个权限为 `0600` 的 closed JSON report，并绑定 exact candidate commit、stage
manifest digest、candidate evidence digest、规范化 review base/range 和 finding 数量。
输出已存在、路径逃逸、symlink、候选变化或范围不匹配都会 fail closed。非零 finding 的
报告仍是有效审查记录，但不能解锁 final；修复后必须从 fresh candidate 重新开始。

## 第二次 fresh final

```bash
M8_LIVE_ACCEPTANCE=1 M8_REVIEW_REPORT="$M8_REVIEW_REPORT_PATH" make release-acceptance
```

预检先读取 report 旁边的原 candidate manifest，验证旧候选证据绑定，再验证当前 commit
和固定命令 manifest 未变化。随后从 fresh state 重跑所有确定性、宿主机、Chromium、
DeepSeek、恢复切换和清理阶段。只有重跑成功且审查为 `0/0/0` 才输出 `final_pass`；最终
证据使用新的 `acceptance_run_id` 和新的 `evidence_relative_locator`。

关闭文档会改变 Git identity，因此首次 `final_pass` 不能证明关闭提交。更新状态文档并提交
后，必须在精确 clean closure commit 上再次执行 candidate → 独立审查 → final 全流程，且
最终运行后不得修改 tracked file。

## 失败、隔离和清理

- 中断、timeout、模型拒绝、恢复失败或任一 gate 失败后，runner 只清理由 invocation
  ledger 证明拥有的 PID/PGID、随机数据库、inode 和临时目录；它不会调用宽泛的
  `make stop`，也不会删除未知资源。
- cleanup residual 非零时立即隔离本次 evidence，不得复用候选或 review report。保留数据库
  或进程的只读身份信息，按 stable error code 排查；不要手工删除所有数据库或工作区。
- secret scanner 一旦发现真实 credential，立即停止发布、隔离证据、撤销或轮换对应
  DeepSeek key、数据库密码、JWT/backup/audit key，并在新 secret 下从 candidate 重新运行。
  不要仅把命中加入 allowlist。
- `.release-evidence/` 必须保持 gitignored、operator-only 访问。按组织发布证据保留策略保存
  candidate、review 和 final manifest；到期后只删除已确认的单个 run 目录，并记录销毁。

## 认证范围

M8 不认证 Docker Compose、Kubernetes、Helm、Firefox、Safari/WebKit 或其他模型供应商。
这些路径需要各自的独立发布验收。M8 `final_pass` 也不等于已经创建 Git tag、推送远端或
发布镜像/Chart；对外发布仍按 [RELEASING.md](../../RELEASING.md) 单独执行。
