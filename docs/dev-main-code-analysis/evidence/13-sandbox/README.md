# Module 13 Sandbox 移植验收证据

验收日期：2026-07-30

## 验收结论

本地完整栈通过 `http://localhost:2026` 完成三轮真实模型调用和 Sandbox 工具调用。
验收始终使用默认项目、系统 `Main` Agent、DeepSeek V4 Pro，以及同一个 Thread：

```text
/projects/default-project/chats/4a03c02c-7ea1-4e89-80c3-a9c58c373129
```

页面显示三轮累计 `136.9K` Tokens。测试没有临时打开 Host bash，也没有切换
`LocalSandboxProvider`；安全策略保持：

```yaml
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
  allow_host_bash: false
```

## 三轮真实模型与工具结果

| 轮次 | 真实工具路径 | 页面可见结果 |
| --- | --- | --- |
| R1 | `write_file` → `read_file(start_line=3)` | 创建 5 行证明文件；只传开始行后准确返回第 3–5 行 |
| R2 | 完整 `read_file` → 空 `old_str` 的 `str_replace` → 实际替换 → 单文件 `grep` | 空字符串调用没有注入内容；`target-old` 改为 `target-new`；单文件 grep 返回虚拟路径与第 3 行 |
| R3 | 刷新同一 Thread → 完整 `read_file` → 两次单文件 `grep` → 尝试 `bash pwd` | 修改跨刷新保留；`SHOULD-NOT-APPEAR-M13` 无匹配；Host bash 明确 fail closed，`pwd` 未执行 |

证明文件为：

```text
/mnt/user-data/workspace/m13-sandbox-proof-20260730.txt
```

最终内容：

```text
line-1: seed-M13-20260730
line-2: keep
line-3: target-new-M13-20260730
line-4: persistence
line-5: end
```

## 截图

| 文件 | 证明内容 |
| --- | --- |
| [01-write-one-sided-read.png](01-write-one-sided-read.png) | `write_file` 后只传 `start_line=3`，返回第 3–5 行；右侧同时显示完整 5 行文件 |
| [02-empty-replace-single-file-grep.png](02-empty-replace-single-file-grep.png) | 单文件 grep 返回虚拟路径、第 3 行和替换后的精确内容 |
| [03-refresh-persistence-empty-guard.png](03-refresh-persistence-empty-guard.png) | 页面刷新后文件仍存在；空 `old_str` 的目标字符串无匹配 |
| [04-bash-fail-closed.png](04-bash-fail-closed.png) | LocalSandboxProvider 明确拒绝 Host bash，`pwd` 未执行 |

截图不包含密码、Cookie、会话令牌、数据库连接串或模型密钥。第三、四张截图左下角
保留了浏览器当前登录账号的截断标签，未显示任何认证材料。

## 自动化门禁

- Module 13 聚焦 Sandbox/Provider 套件：`664 passed, 7 skipped`
- 后续 AIO grep 分页、空截断、单文件契约回归：`29 passed`
- 路径前缀兄弟与合法边界回归：`11 passed`
- blocking-I/O 完整门禁：`26 passed`
- 后端完整测试：`7675 passed, 1014 skipped, 0 failed`
- M1–M7 真实 PostgreSQL 门禁：`270 passed, 0 skipped`
- Ruff check、Ruff format check、`git diff --check`：最终收口时执行

PostgreSQL 门禁只创建和删除随机 `deerflow_test_*` 数据库，没有修改业务数据库。

## 未宣称通过的目标环境

本次浏览器证据只证明本地 `LocalSandboxProvider` 路径。Kubernetes Helm
Provisioner 不在本次通过范围：

- 当前 Chart 没有 Worker Deployment；
- Provisioner `/api/*` 已改为 API key fail closed；
- 密钥不能为“让 Chart 表面可用”而交给不执行 Agent 图的 Gateway；
- 在 Module 16/Helm 补齐 Worker 与 Worker-only Secret 注入前，不能宣称 Helm
  Provisioner 可用。

此外，E2B 当前完成的是 size + remote mtime 的同大小变化检测，不包含 `main` 的
sandbox-ID 绑定同步 manifest；远端私有文件回写仍必须按 Project/Owner/Run/Worker
authority 重写。
