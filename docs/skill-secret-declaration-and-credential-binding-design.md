# Skill 凭据声明、映射与激活设计

状态：当前实现约束
范围：Project Skill、压缩包导入、AI Skill Builder、Credential 映射、版本激活

## 目标

Skill 文件通过 `SKILL.md` frontmatter 声明运行时需要的环境变量，项目侧把每个目标
变量映射到一个精确 Project Credential version 的安全 `env` 来源字段。文件内容、
映射元数据和明文密钥保持三个独立边界。

Project Skill 保存后生成不可变 Candidate Version。保存不改变运行行为；显式激活通过
只读运行条件检查后原子设置 `current_version_id` 并启用资产。Historical Version 只读，
不能编辑、删除、复制或再次激活。

## 权威边界

1. `SKILL.md` 是 `required-secrets` 与 `secrets-autonomous` 的唯一声明来源。
2. Credential 映射只存 PostgreSQL，不写入 Skill 文件、浏览器缓存或 Run 事件。
3. Credential 明文只在 Worker 执行已准入 Run Snapshot 时解密并注入进程环境。
4. Editor 只能查看声明、映射完整性与失效状态；Admin 管理 Credential 和来源字段。
5. Agent 只绑定 Skill Asset ID。Run Admission 解析 Skill Current Version 和对应映射。

## 保存版本

- 压缩包导入和 AI Builder 首次提交创建 suspended Skill 与 Candidate Version v1。
- 后续版本只能从最新向前 head 创建，版本号单调递增。
- 新 Candidate Version 会继承上一 head 中声明仍存在、来源字段仍兼容且 Credential
  仍有效的映射；其余项标记为缺失或失效。
- 保存事务固定文件字节、frontmatter、静态扫描结论、声明列表、校验和与继承结果。
- 本地未保存修改和 Builder 候选文件不是资产版本，离开确认和冲突恢复不得把它们
  表述为版本状态。

## 映射 API

```text
GET /api/projects/{project_id}/skills/{skill_id}/versions/{version_id}/credential-bindings
PUT /api/projects/{project_id}/skills/{skill_id}/versions/{version_id}/credential-bindings
GET /api/projects/{project_id}/skills/{skill_id}/versions/{version_id}/activation-readiness
POST /api/projects/{project_id}/skills/{skill_id}/versions/{version_id}/activate
```

映射写入使用映射 revision CAS。请求只提交 Credential/version ID 与来源字段名，不提交
明文。Historical Version 拒绝写入；Candidate Version 与 Current Version 可在权限允许时
轮换映射。

## 激活安全门

激活请求固定以下只读检查返回的身份：

- Asset `revision`
- Candidate `payload_checksum`
- Credential mapping `binding_revision`

服务端在同一事务重新锁定并校验：

1. 目标属于当前项目，且关系仍是 Candidate Version。
2. 目标是从当前 forward head 派生的合法候选，不存在内容回退或旧分支覆盖。
3. 文件归档、frontmatter、声明和静态扫描元数据与校验和一致。
4. 所有必需映射完整；不存在失效的必需或可选映射。
5. Credential version 仍有效、字段仍兼容，且调用方权限满足生命周期操作。
6. Skill 运行名在 Project Skill 与已启用 System Skill 之间仍唯一。

检查通过后，事务设置 `current_version_id`、将资产设为 active、增加 revision 并写审计。
被跳过及更早版本成为 Historical Version。资产之后可单独 suspended；重新启用保持同一
Current Version。

## 并发与失败

- 文件解析、表单 patch、保存、映射和激活均使用 revision/checksum CAS。
- 任一检查失败都不移动 Current Version，不产生部分激活，也不丢弃浏览器未保存修改。
- Credential 创建成功但映射保存失败时，只保留已创建 Credential；用户可重新选择并保存。
- Current Version 在 Builder 修订期间变化时，该会话不能覆盖保存。用户必须从新的
  Current Version 创建修订会话。

## Run 语义

每个 Run Admission 都重新解析 Agent 与 Skill 的 Current Version，包括既有 Thread 的
后续消息、编辑/重新生成、分叉、Automation 和 Channel。准入把精确 Skill 文件、映射、
Credential version 与策略写入完整 Run Snapshot。Worker 只执行该快照；运行和 Job Attempt
重试期间不会重新查询 Current Version 或 Credential 映射。

## 验收重点

- 保存 v1 后资产 suspended 且尚无 Current Version；激活 v1 后同时成为 Current 并启用。
- 保存 v2 不影响新 Run；激活 v2 后新 Run 使用 v2，已准入 Run 继续使用原快照。
- 既有 Thread 的下一条消息使用激活后的 Agent/Skill Current Version。
- v1 Current、v2/v3 Candidate 时可直接激活 v3；v1/v2 随后只读且不能再次激活。
- 从历史内容创建更高版本、编辑历史、删除历史及旧基线覆盖都被拒绝。
- 兼容 Credential 映射自动继承；缺失映射时 Editor 只看到完整性并提示联系 Admin。
- 激活失败不改变 Current Version、资产状态、映射或审计边界。
