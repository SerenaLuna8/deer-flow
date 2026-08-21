#!/usr/bin/env python3
"""Generate deterministic Chinese PostgreSQL table and column comments.

The fresh-install schema is intentionally the authority for relation order and
column membership.  Human-maintained table descriptions and a small field-name
glossary provide the semantics; this script joins the two into explicit
``COMMENT ON`` statements.  Run with ``--check`` to reject a stale generated
artifact after any schema change.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_PATH = _REPOSITORY_ROOT / "backend" / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"
_OUTPUT_PATH = _SCHEMA_PATH.with_name("schema_comments.sql")
_BLOCK_START = "-- BEGIN GENERATED SCHEMA COMMENTS"
_BLOCK_END = "-- END GENERATED SCHEMA COMMENTS"

# These counts deliberately describe static CREATE TABLE statements only.  The
# monthly run_events child partitions are created dynamically and therefore are
# outside this static-schema artifact.
_EXPECTED_TABLE_COUNT = 93
_EXPECTED_COLUMN_COUNT = 1147

_CREATE_TABLE_RE = re.compile(r"^CREATE TABLE ([a-z][a-z0-9_]*) \($")
_COLUMN_RE = re.compile(r"^ {4}([a-z][a-z0-9_]*)\s+")


class SchemaCommentError(ValueError):
    """Raised when schema structure or comment metadata is incomplete."""


@dataclass(frozen=True, slots=True)
class TableDefinition:
    name: str
    columns: tuple[str, ...]


# The short label is reused as column-comment context.  The longer description
# explains ownership/lifecycle at table level.  Keep every static table explicit
# so a new table cannot silently receive a vague generated description.
_TABLE_METADATA: dict[str, tuple[str, str]] = {
    "alembic_version": ("数据库迁移版本", "记录当前数据库采用的 Alembic 架构版本。"),
    "asset_catalog_state": ("资产目录状态", "记录系统资产目录的单例代次与更新时间。"),
    "system_asset_upgrade_audit": (
        "系统资产升级审计",
        "记录软件包升级原子替换 System Agent 或 Skill Current v1 的校验和证据。",
    ),
    "dead_jobs": ("死信任务", "保存超过重试边界或无法安全重试的后台任务终态。"),
    "execution_approval_requests": (
        "执行审批请求",
        "保存本机命令的一次性审批、领取与终态生命周期。",
    ),
    "execution_approval_result_receipts": (
        "执行审批结果回执",
        "保存一次已审批本机命令的有界私有执行结果。",
    ),
    "execution_approval_output_delivery_obligations": (
        "审批输出交付义务",
        "保存审批暂停后必须由续接运行完成的私有输出交付义务。",
    ),
    "execution_approval_output_delivery_candidates": (
        "审批输出交付候选",
        "冻结可满足审批输出交付义务的私有文件身份与版本。",
    ),
    "jobs": ("后台任务", "保存 Worker 可领取、续租、重试和结算的持久化任务。"),
    "project_invitation_rate_limits": ("项目邀请限流", "记录项目邀请码失败尝试的限流窗口。"),
    "runs": ("智能体运行", "保存一次智能体运行的身份、状态、用量与执行租约。"),
    "scheduled_task_runs": ("调度任务运行", "保存自动化任务每次计划或手动触发的运行记录。"),
    "users": ("用户", "保存平台用户、渠道访客及其登录与偏好状态。"),
    "auth_sessions": ("认证会话", "保存可撤销的用户认证会话及其有效期。"),
    "worker_nodes": ("工作节点", "记录 Worker 节点能力、容量与心跳状态。"),
    "job_attempts": ("任务尝试", "记录后台任务每次领取和执行尝试的结算信息。"),
    "projects": ("项目", "保存项目基本信息、生命周期与所有者治理状态。"),
    "agents": ("项目智能体", "保存智能体的逻辑身份和 Current Version 指针。"),
    "project_default_agents": ("项目默认智能体", "保存项目范围内默认智能体的唯一绑定。"),
    "audit_logs": ("审计日志", "保存脱敏且不可变的安全与治理操作审计事件。"),
    "credentials": ("受管凭据", "保存项目受管凭据的逻辑身份和当前版本指针。"),
    "project_channel_instances": ("项目渠道实例", "保存项目接入渠道实例及其期望运行状态。"),
    "project_channel_instance_leases": ("渠道实例租约", "保存渠道实例运行者的栅栏代次与租约。"),
    "mcp_servers": ("项目 MCP 服务", "保存项目 MCP 服务的逻辑身份和当前发布指针。"),
    "project_invitations": ("项目邀请", "保存项目成员邀请、兑换和撤销生命周期。"),
    "user_notifications": ("用户通知", "保存面向用户的站内通知及已读状态。"),
    "project_memberships": ("项目成员关系", "保存用户在项目中的角色、能力版本与停用状态。"),
    "project_channel_group_binding_challenges": ("渠道群组绑定验证", "保存外部渠道群组绑定前的短期验证挑战。"),
    "project_channel_group_bindings": (
        "渠道群组绑定",
        "保存外部渠道群组与项目之间的受管绑定及可复用身份锚点。",
    ),
    "channel_external_principals": ("渠道外部主体", "保存渠道外部身份到平台主体的映射。"),
    "project_quotas": ("项目配额", "保存项目可收紧的平台资源限额。"),
    "project_usage_counters": ("项目用量计数", "保存项目当前计量桶中的事务性用量。"),
    "project_usage_ledger": ("项目用量台账", "保存项目已提交用量变化的追加式台账。"),
    "skills": ("项目技能", "保存技能的逻辑身份和 Current Version 指针。"),
    "agent_versions": ("智能体版本", "保存不可变的项目智能体版本内容与运行配置。"),
    "agent_design_sessions": ("智能体设计会话", "保存智能体设计向导的私有会话状态与产物引用。"),
    "agent_design_operations": ("智能体设计操作", "保存智能体设计会话中的幂等操作及其结果。"),
    "agent_design_activities": ("智能体设计活动", "保存智能体设计会话中可回放的公开过程事件。"),
    "channel_connections": ("渠道连接", "保存用户授权的外部渠道账户连接。"),
    "channel_oauth_states": ("渠道 OAuth 状态", "保存渠道 OAuth 流程的一次性校验状态。"),
    "credential_versions": ("凭据版本", "保存受管凭据的不可变版本元数据。"),
    "feedback": ("运行反馈", "保存用户针对智能体运行提交的评分与意见。"),
    "mcp_server_versions": ("MCP 服务版本", "保存不可变的 MCP 服务连接与公开配置。"),
    "run_asset_versions": ("运行资产快照", "冻结一次运行准入时解析出的智能体、技能或 MCP 完整版本内容。"),
    "run_event_partition_state": ("运行事件分区状态", "记录运行事件分区维护的高水位。"),
    "run_event_invariants": ("运行事件不变量", "保存运行事件全局单调序列的单例状态。"),
    "run_events": ("运行事件", "保存按日期分区的持久化运行事件流。"),
    "skill_versions": ("技能版本", "保存不可变的项目技能版本及扫描结论。"),
    "threads_meta": ("线程元数据", "保存项目私有线程的标题、状态与活动时间。"),
    "agent_version_mcp_refs": ("智能体 MCP 引用", "保存智能体版本到 MCP 服务版本的有序依赖。"),
    "agent_version_skill_refs": ("智能体技能引用", "保存智能体版本到技能资产的有序依赖；运行时解析其 Current Version。"),
    "channel_conversations": ("渠道会话", "映射外部渠道会话与项目私有线程。"),
    "channel_inbound_deliveries": ("渠道入站投递", "保存渠道入站消息的幂等接收与处理状态。"),
    "channel_credentials": ("渠道令牌凭据", "保存渠道连接令牌的加密材料与有效期。"),
    "credential_envelopes": ("凭据信封", "保存凭据版本的受管加密信封。"),
    "files": ("项目文件", "保存项目私有文件的身份、存储元数据与生命周期。"),
    "mcp_version_credential_slots": ("MCP 凭据槽位", "声明 MCP 服务版本所需的受管凭据槽位。"),
    "mcp_tool_discovery_attempts": ("MCP 工具发现尝试", "记录 MCP 工具清单发现任务的尝试与结论。"),
    "project_mcp_tool_inventories": ("项目 MCP 工具清单", "保存项目 MCP 服务版本最近发现的诊断性工具清单。"),
    "project_system_agent_bindings": ("项目系统智能体绑定", "保存项目对系统智能体资产的启用绑定。"),
    "project_system_mcp_bindings": ("项目系统 MCP 绑定", "保存项目对系统 MCP 资产的启用绑定。"),
    "project_system_skill_bindings": ("项目系统技能绑定", "保存项目对系统技能资产的启用绑定。"),
    "scheduled_tasks": ("自动化任务", "保存项目自动化任务定义、计划与并发策略。"),
    "skill_version_files": ("技能版本文件", "保存技能版本归档内文件的路径与内容。"),
    "thread_event_sequences": ("线程事件序列", "保存每个私有线程下一条事件序号的单例状态。"),
    "artifacts": ("运行制品", "保存运行生成制品的逻辑身份与存储元数据。"),
    "credential_grants": ("凭据授权", "保存项目凭据向智能体或 MCP 目标的授权。"),
    "file_chunks": ("文件分块", "保存项目文件的有序二进制分块。"),
    "run_mcp_grant_snapshots": ("运行 MCP 授权快照", "冻结运行使用 MCP 服务时的凭据授权闭包。"),
    "skill_design_sessions": ("技能设计会话", "保存技能设计向导的私有会话状态与产物引用。"),
    "skill_design_operations": ("技能设计操作", "保存技能设计会话中的幂等操作及其结果。"),
    "skill_design_activities": ("技能设计活动", "保存技能设计会话中可回放的公开思考与执行过程。"),
    "skill_design_operation_baseline_files": (
        "技能设计操作基线文件",
        "保存技能生成轮次开始前用于停止或失败回滚的草稿快照。",
    ),
    "skill_design_draft_files": ("技能设计草稿文件", "保存技能设计会话当前草稿中的文件内容。"),
    "project_channel_credential_bindings": ("渠道凭据绑定", "保存项目渠道实例使用的受管凭据版本绑定。"),
    "project_skill_credential_configs": ("技能凭据配置", "保存项目技能版本凭据配置的修订状态。"),
    "project_skill_credential_bindings": ("技能凭据绑定", "保存技能凭据槽位到受管凭据版本的绑定。"),
    "run_skill_credential_snapshots": ("运行技能凭据快照", "冻结运行使用技能时的凭据绑定闭包。"),
    "system_model_catalog_state": ("系统模型目录状态", "记录系统模型目录的单例修订号。"),
    "system_model_configs": (
        "系统模型配置",
        "保存系统模型配置的稳定标识、展示名称和当前版本指针。",
    ),
    "system_model_config_versions": ("系统模型配置版本", "保存不可变的系统模型提供方与能力配置。"),
    "run_model_config_snapshots": ("运行模型配置快照", "冻结一次运行采用的模型配置版本。"),
    "system_runtime_policy_catalog_state": ("系统运行策略目录状态", "记录系统运行策略目录的单例修订号。"),
    "system_runtime_policies": ("系统运行策略", "保存各策略节当前采用的版本指针。"),
    "system_runtime_policy_versions": ("系统运行策略版本", "保存不可变的系统运行策略载荷。"),
    "run_runtime_policy_snapshots": ("运行策略快照", "冻结一次运行采用的系统运行策略版本。"),
    "memory_documents": ("记忆文档", "保存项目用户命名空间下的当前结构化记忆文档。"),
    "memory_history_entries": ("记忆历史条目", "保存等待整理或已消费的记忆输入历史。"),
    "memory_dream_runs": ("记忆整理运行", "保存一次记忆整理任务的输入范围与结算版本。"),
    "memory_dream_prepare_runs": (
        "记忆整理准备运行",
        "保存线程消息排空、进度恢复与子记忆整理任务准入的持久化状态。",
    ),
    "memory_document_versions": ("记忆文档版本", "保存记忆文档每次变更的不可变版本与差异。"),
    "memory_episodes": ("记忆片段", "保存可检索的历史记忆片段归档。"),
    "run_memory_context_snapshots": ("运行记忆上下文快照", "冻结一次运行注入的记忆文档内容。"),
}


# Exact phrases cover security-sensitive and frequently reused fields where a
# token-by-token rendering would be ambiguous.  They describe purpose only and
# never include an example, secret, ciphertext, token, or private identifier.
_COLUMN_PHRASES: dict[str, str] = {
    "id": "主键标识",
    "version_num": "数据库迁移版本号",
    "project_id": "所属项目标识",
    "owner_user_id": "私有数据所有者的用户标识",
    "user_id": "用户标识",
    "created_by_user_id": "创建操作的用户标识",
    "created_by_run_id": "生成该记录的运行标识",
    "updated_by_user_id": "最近更新操作的用户标识",
    "requested_by_user_id": "发起请求的用户标识",
    "reviewed_by_user_id": "执行审核的用户标识",
    "revoked_by_user_id": "执行撤销的用户标识",
    "revocation_reason_code": "撤销原因代码",
    "redeemed_by_user_id": "兑换邀请的用户标识",
    "ended_by_user_id": "结束流程的用户标识",
    "deletion_requested_by_user_id": "请求删除的用户标识",
    "created_at": "记录创建时间",
    "updated_at": "记录最近更新时间",
    "spawn_authorized_at": "一次性进程创建授权提交时间",
    "deleted_at": "记录删除时间",
    "status": "生命周期状态",
    "version": "记录版本号",
    "revision": "配置修订号",
    "generation": "单调代次",
    "sequence": "单调序号",
    "seq": "单调序号",
    "scope": "资产或数据归属范围",
    "role": "授权角色",
    "kind": "业务类型",
    "provider": "外部服务提供方",
    "trigger": "触发方式",
    "outcome": "执行结果",
    "action": "审计操作类型",
    "priority": "任务领取优先级",
    "max_attempts": "允许的最大执行尝试次数",
    "max_concurrent_jobs": "可并发执行的任务上限",
    "desired_status": "控制面期望状态",
    "observed_status": "运行面观测状态",
    "name": "名称",
    "display_name": "展示名称",
    "description": "用途描述",
    "slug": "稳定可读标识名",
    "namespace": "私有数据命名空间",
    "email": "规范化邮箱地址",
    "username": "登录用户名",
    "password_hash": "密码验证哈希（不存储明文密码）",
    "session_id_hash": "认证会话标识的不可逆哈希",
    "token_hash": "令牌的不可逆哈希",
    "lease_token_hash": "执行租约令牌的不可逆哈希",
    "execution_lease_token_hash": "运行租约令牌的不可逆哈希",
    "ciphertext": "由受管密钥保护的密文",
    "encrypted_access_token": "加密保存的访问令牌",
    "encrypted_refresh_token": "加密保存的刷新令牌",
    "code_verifier_encrypted": "加密保存的 OAuth 校验器",
    "encrypted_extra_json": "加密保存的扩展认证数据",
    "nonce": "加密信封使用的随机数",
    "nonce_hash": "一次性随机数的不可逆哈希",
    "content": "正文或二进制内容",
    "tagged_text": "带来源标签的记忆文本",
    "metadata_json": "非敏感业务元数据",
    "token_usage_by_model": "按模型汇总的令牌用量",
    "follow_up_to_run_id": "被当前运行跟进的前序运行标识",
    "total_input_tokens": "累计输入令牌数量",
    "total_output_tokens": "累计输出令牌数量",
    "total_tokens": "累计令牌总量",
    "lead_agent_tokens": "主智能体消耗的令牌数量",
    "subagent_tokens": "子智能体消耗的令牌数量",
    "middleware_tokens": "中间件消耗的令牌数量",
    "kwargs_json": "冻结的运行关键字参数",
    "messages_json": "设计会话消息列表",
    "progress_json": "设计会话进度列表",
    "validation_json": "草稿验证结果",
    "capabilities_json": "工作节点能力列表",
    "requested_scopes_json": "请求的 OAuth 权限范围",
    "scopes_json": "已授予的 OAuth 权限范围",
    "public_config": "不含机密值的公开配置",
    "non_secret_headers": "不含机密值的请求头配置",
    "non_secret_env": "不含机密值的环境变量配置",
    "secret_requirements": "所需机密项的名称与用途声明",
    "payload_schema": "载荷结构定义",
    "payload_checksum": "载荷内容校验和",
    "content_digest": "内容摘要",
    "source_digest": "来源内容摘要",
    "source_env_field_name": "来源环境变量字段名称",
    "state_hash": "状态内容哈希",
    "sha256": "内容的 SHA-256 摘要",
    "idempotency_key": "幂等操作键",
    "idempotency_key_hash": "幂等操作键的不可逆哈希",
    "create_idempotency_key_hash": "创建操作幂等键的不可逆哈希",
    "manual_idempotency_hash": "手动触发幂等键的不可逆哈希",
    "origin_trace_id": "跨组件关联运行的追踪标识",
    "operation_id": "操作标识",
    "request_id": "请求标识",
    "phase": "阶段",
    "compacted_passes": "压缩轮次",
    "last_checkpoint_id": "最近检查点标识",
    "dream_job_id": "记忆整理任务标识",
    "admission_kind": "准入类型",
    "admission_only": "仅供已准入运行继续验证的退役权限标记",
    "runtime_authority_binding_id": "退役绑定关联的当前运行权限绑定标识",
    "result_disposition": "结果处置",
    "owner_ref_hmac": "所有者引用的域分离 HMAC",
    "source_ref_hmac": "来源引用的域分离 HMAC",
    "target_ref_hmac": "目标引用的域分离 HMAC",
    "provider_identity_digest": "外部提供方身份的脱敏摘要",
    "provider_delivery_digest": "外部投递标识的脱敏摘要",
    "attempt_grant_digest": "本次尝试授权闭包的摘要",
    "grant_digest": "凭据授权闭包的摘要",
    "tools_grant_digest": "工具授权闭包的摘要",
    "first_human_message": "首条用户消息的运行摘要内容",
    "last_ai_message": "末条智能体消息的运行摘要内容",
    "error": "内部运行错误详情",
    "error_code": "稳定错误代码",
    "public_error_code": "可公开的稳定错误代码",
    "error_message": "受限的错误说明",
    "rating": "用户评分",
    "comment": "用户反馈说明",
    "is_active": "是否处于启用状态",
    "is_delete": "是否标记为删除",
    "is_pinned": "是否置顶",
    "is_stream_terminal": "是否为事件流终态",
    "is_suspended": "是否暂停成员权限",
    "enabled": "是否启用",
    "memory_enabled": "是否启用用户记忆功能",
    "draining": "是否处于排空状态",
    "created_agent_deleted": "已创建的智能体是否随后删除",
    "generation_model_ref": "生成模型引用",
    "generation_mode": "生成模式",
    "stop_requested_at": "停止请求时间",
    "requested_generation_profile_json": "请求生成配置 JSON 数据",
    "effective_generation_profile_json": "生效生成配置 JSON 数据",
    "payload_json": "公开载荷 JSON 数据",
    "created_skill_deleted": "已创建的技能是否随后删除",
    "required": "该凭据槽位是否必需",
    "reserved": "预留计量数量",
    "used": "已使用计量数量",
    "singleton": "单例约束标识",
    "sections": "有序的记忆文档章节名称",
    "sections_policy_section": "约束章节结构的运行策略节",
    "sections_policy_version_id": "约束章节结构的运行策略版本标识",
    "timezone": "计划解释所用时区",
    "scheduled_for": "本次任务计划触发时间",
    "redirect_after": "OAuth 完成后的站内跳转路径",
    "retention_until": "成员私有数据的保留截止时间",
    "retained_from": "运行事件当前保留范围的起始日期",
    "history_from": "本次记忆整理的起始历史序号",
    "history_to": "本次记忆整理的结束历史序号",
    "high_watermark": "已经分配的最大事件序号",
    "schedule_spec": "调度计划表达式",
    "tool_groups": "允许使用的工具分组",
    "tool_overrides": "工具级策略覆盖配置",
    "tools": "发现或配置的工具列表",
    "model_settings": "模型调用设置",
    "settings": "运行策略设置",
    "user_context": "智能体使用的用户上下文",
    "agents_instructions": "项目智能体行为指令",
    "soul": "项目智能体人格设定",
    "frontmatter": "技能入口文件的元数据头",
    "unified_diff": "相对上一记忆版本的统一格式差异",
    "logical_path": "文件在项目中的逻辑路径",
    "event_metadata": "事件的结构化非敏感元数据",
    "artifact_metadata": "制品的结构化非敏感元数据",
    "oauth_metadata": "OAuth 能力的非敏感元数据",
    "review_note": "静态审核说明",
    "scan_summary": "安全扫描摘要",
    "scan_decision": "安全扫描结论",
    "url": "不含凭据的服务访问地址",
    "source_job_id": "产生审批请求的任务标识",
    "source_job_attempt_id": "产生审批请求的任务尝试标识",
    "source_agent_path": "产生命令的智能体调用路径",
    "tool_call_id": "产生命令的工具调用标识",
    "command_digest": "规范化私有命令的内容摘要",
    "execution_domain_affinity": "执行域私有快照的不可逆亲和摘要",
    "decision": "一次性审批决定",
    "decision_idempotency_key": "审批决定的幂等键摘要",
    "decision_request_digest": "审批决定请求的内容摘要",
    "decided_by_user_id": "作出审批决定的用户标识",
    "decided_at": "审批决定时间",
    "continuation_run_id": "审批通过后续接运行的标识",
    "continuation_job_id": "审批通过后续接任务的标识",
    "execution_job_id": "执行已审批命令的任务标识",
    "execution_job_attempt_id": "执行已审批命令的任务尝试标识",
    "claimed_at": "已审批命令的领取时间",
    "terminal_at": "审批请求进入终态的时间",
    "approval_id": "执行审批请求标识",
    "exit_code": "命令进程退出代码",
    "result_digest": "有界私有执行结果的内容摘要",
    "mode": "履约模式",
    "intent_tool_call_id": "记录输出交付意图的工具调用标识",
    "intent_digest": "规范化私有输出交付意图的内容摘要",
    "intent_private_json": "仅限授权边界读取的输出交付意图 JSON",
    "satisfied_artifact_id": "满足输出交付义务的运行制品标识",
    "assigned_at": "输出交付义务分配给续接运行的时间",
    "intent_recorded_at": "输出交付意图持久化的时间",
    "file_version": "候选文件的冻结版本号",
}

# Reused column names can carry materially different privacy and storage
# semantics. Table-specific entries take precedence over the shared glossary.
_TABLE_COLUMN_PHRASES: dict[tuple[str, str], str] = {
    ("system_asset_upgrade_audit", "before_checksum"): "升级前载荷校验和",
    ("system_asset_upgrade_audit", "after_checksum"): "升级后载荷校验和",
    ("system_asset_upgrade_audit", "package_digest"): "升级软件包目录摘要",
    ("system_asset_upgrade_audit", "operator_identity"): "执行数据库升级的操作主体身份",
    ("run_asset_versions", "snapshot_json"): "准入时冻结的完整且不含明文凭据的资产内容",
    (
        "jobs",
        "execution_domain_affinity",
    ): "限制本机命令续接任务的执行域亲和摘要",
    ("skill_versions", "revoked_at"): "不可逆治理撤销时间",
    ("project_channel_group_bindings", "agent_scope"): "活动绑定的智能体范围；软删除后为空",
    ("project_channel_group_bindings", "agent_asset_id"): "活动绑定的智能体资产标识；软删除后为空",
    ("project_channel_group_bindings", "deleted_at"): "软删除时间；置值后保留身份锚点并释放智能体引用",
    ("runs", "first_human_message"): "首条用户消息文本的截断副本（最多 2000 字符，属于私有内容）",
    ("runs", "last_ai_message"): "末条主智能体可展示消息文本的截断副本（最多 2000 字符，属于私有内容）",
    ("run_events", "content"): "事件正文文本（可能包含私有消息、轨迹或生命周期内容）",
    ("skill_version_files", "content"): "技能版本文件的原始字节内容",
    ("file_chunks", "content"): "文件分块的原始字节内容",
    ("skill_design_draft_files", "content"): "技能设计草稿文件的原始字节内容",
    ("skill_design_operation_baseline_files", "content"): "技能设计操作基线文件的原始字节内容",
    ("memory_documents", "content"): "当前结构化记忆文档正文（属于私有内容）",
    ("memory_document_versions", "content"): "该版本的结构化记忆文档正文（属于私有内容）",
    ("run_memory_context_snapshots", "content"): "运行时冻结的记忆文档正文（属于私有内容）",
    (
        "execution_approval_requests",
        "command_private_json",
    ): "规范化且仅限授权边界读取的私有命令计划 JSON（最多 1 MiB）",
    (
        "execution_approval_requests",
        "source_run_id",
    ): "产生审批请求的运行标识",
    (
        "execution_approval_requests",
        "expires_at",
    ): "审批请求过期时间",
    (
        "execution_approval_result_receipts",
        "result_private_json",
    ): "仅限授权边界读取的有界命令结果 JSON（最多 2 MiB）",
    (
        "execution_approval_result_receipts",
        "outcome",
    ): "命令启动或完成结果",
    (
        "execution_approval_output_delivery_obligations",
        "intent_private_json",
    ): "仅限授权边界读取的规范化输出交付意图 JSON（最多 1 MiB）",
    (
        "execution_approval_output_delivery_obligations",
        "terminal_at",
    ): "输出交付义务进入终态的时间",
}


# Used by the deterministic fallback.  Unknown words fail generation instead of
# leaking an unexplained English identifier into a nominally Chinese comment.
_WORD_LABELS: dict[str, str] = {
    "access": "访问",
    "account": "账户",
    "acted": "操作发生",
    "action": "操作",
    "activated": "启用",
    "activation": "启用",
    "active": "活跃",
    "activity": "活动",
    "actor": "操作主体",
    "adapter": "适配器",
    "after": "后续跳转",
    "agent": "智能体",
    "agents": "智能体",
    "ai": "智能体",
    "args": "参数",
    "artifact": "制品",
    "asset": "资产",
    "assistant": "助手",
    "attempt": "尝试",
    "authoring": "编写用途",
    "attempts": "尝试",
    "authorization": "授权",
    "automation": "自动化",
    "available": "可用",
    "base": "基线",
    "binding": "绑定",
    "blueprint": "蓝图",
    "bot": "机器人",
    "bucket": "计量桶",
    "by": "按",
    "bytes": "字节数",
    "call": "调用",
    "calls": "调用",
    "cancel": "取消",
    "capabilities": "能力",
    "catalog": "目录",
    "category": "类别",
    "channel": "渠道",
    "checkpoint": "检查点",
    "checksum": "校验和",
    "chunk": "分块",
    "ciphertext": "密文",
    "clarification": "澄清",
    "code": "代码",
    "command": "命令",
    "comment": "说明",
    "committed": "已提交",
    "compatibility": "兼容性",
    "completed": "完成",
    "concurrent": "并发",
    "config": "配置",
    "connection": "连接",
    "consumed": "消费",
    "content": "内容",
    "context": "上下文",
    "conversation": "会话",
    "count": "数量",
    "create": "创建",
    "created": "创建",
    "creator": "创建器",
    "credential": "凭据",
    "current": "当前",
    "cursor": "游标",
    "daily": "每日",
    "dead": "死信",
    "decision": "决策",
    "default": "默认",
    "delete": "删除",
    "deleted": "已删除",
    "deletion": "删除",
    "delivery": "投递",
    "delta": "增量",
    "dependency": "依赖",
    "dependencies": "依赖",
    "description": "描述",
    "desired": "期望",
    "diff": "差异",
    "digest": "摘要",
    "dimension": "维度",
    "display": "展示",
    "document": "文档",
    "draft": "草稿",
    "draining": "排空",
    "dream": "记忆整理",
    "effective": "生效",
    "effort": "强度",
    "email": "邮箱",
    "enabled": "启用",
    "encrypted": "加密",
    "end": "结束",
    "ended": "结束",
    "entered": "进入",
    "env": "环境变量",
    "envelope": "信封",
    "error": "错误",
    "event": "事件",
    "execution": "执行",
    "expires": "过期",
    "external": "外部",
    "extra": "扩展",
    "failure": "失败",
    "feedback": "反馈",
    "fencing": "栅栏",
    "file": "文件",
    "finalization": "收尾",
    "finished": "完成",
    "first": "首次",
    "follow": "跟进",
    "for": "目标",
    "from": "起始",
    "frontmatter": "元数据头",
    "frozen": "冻结",
    "generation": "代次",
    "grant": "授权",
    "group": "群组",
    "groups": "分组",
    "hash": "哈希",
    "headers": "请求头",
    "heartbeat": "心跳",
    "high": "高位",
    "history": "历史",
    "hmac": "HMAC",
    "holder": "持有者",
    "human": "用户",
    "icon": "图标",
    "id": "标识",
    "idempotency": "幂等",
    "identity": "身份",
    "index": "序号",
    "input": "输入",
    "instance": "实例",
    "instructions": "指令",
    "invitation": "邀请",
    "invited": "受邀",
    "is": "是否",
    "job": "任务",
    "jobs": "任务",
    "json": "JSON 数据",
    "key": "键",
    "kind": "类型",
    "kwargs": "关键字参数",
    "last": "最近",
    "launch": "启动",
    "lead": "主",
    "lease": "租约",
    "limit": "限额",
    "llm": "大模型",
    "logical": "逻辑",
    "manual": "手动",
    "max": "最大",
    "mcp": "MCP",
    "media": "媒体",
    "member": "成员",
    "membership": "成员关系",
    "memory": "记忆",
    "message": "消息",
    "messages": "消息",
    "metadata": "元数据",
    "middleware": "中间件",
    "mode": "模式",
    "model": "模型",
    "multitask": "多任务",
    "name": "名称",
    "namespace": "命名空间",
    "needs": "需要",
    "next": "下次",
    "non": "非敏感",
    "nonce": "随机数",
    "note": "说明",
    "num": "编号",
    "number": "编号",
    "oauth": "OAuth",
    "observed": "观测",
    "occurred": "发生",
    "occurrence": "触发实例",
    "operation": "操作",
    "order": "顺序",
    "origin": "来源",
    "outcome": "结果",
    "output": "输出",
    "overlap": "重叠",
    "overrides": "覆盖项",
    "owner": "所有者",
    "password": "密码",
    "path": "路径",
    "passes": "轮次",
    "phase": "阶段",
    "prepare": "准备",
    "payload": "载荷",
    "pinned": "置顶",
    "platform": "平台",
    "policy": "策略",
    "predecessor": "前序",
    "preference": "偏好",
    "preferences": "偏好",
    "principal": "主体",
    "priority": "优先级",
    "process": "进程",
    "progress": "进度",
    "project": "项目",
    "prompt": "提示词",
    "provider": "提供方",
    "public": "公开",
    "published": "发布",
    "purpose": "用途",
    "rating": "评分",
    "read": "已读",
    "reason": "原因",
    "reasoning": "推理",
    "recipient": "接收者",
    "redeemed": "兑换",
    "redirect": "重定向",
    "ref": "引用",
    "refresh": "刷新",
    "request": "请求",
    "requested": "请求",
    "required": "必需",
    "requirements": "要求",
    "reserved": "预留",
    "resolved": "解析",
    "result": "结果",
    "retained": "保留",
    "retention": "保留",
    "retired": "停用",
    "retry": "重试",
    "review": "审核",
    "reviewed": "审核",
    "revision": "修订",
    "revoked": "撤销",
    "role": "角色",
    "rotated": "轮换",
    "routing": "路由",
    "run": "运行",
    "safety": "安全性",
    "scan": "扫描",
    "schedule": "调度",
    "scheduled": "计划",
    "schema": "架构",
    "scope": "范围",
    "scopes": "权限范围",
    "sealed": "封存",
    "seconds": "秒数",
    "secret": "机密项",
    "section": "策略节",
    "sections": "章节",
    "seen": "发现",
    "seq": "序号",
    "sequence": "序列",
    "server": "服务",
    "session": "会话",
    "settings": "设置",
    "setup": "初始化",
    "sha256": "SHA-256",
    "singleton": "单例",
    "size": "大小",
    "skill": "技能",
    "slot": "槽位",
    "slug": "标识名",
    "snip": "摘录",
    "sort": "排序",
    "soul": "人格设定",
    "source": "来源",
    "spec": "表达式",
    "started": "开始",
    "state": "状态",
    "status": "状态",
    "storage": "存储",
    "strategy": "策略",
    "stream": "事件流",
    "subagent": "子智能体",
    "submitted": "提交",
    "success": "成功",
    "summary": "摘要",
    "supersedes": "替代目标",
    "supports": "支持",
    "suspended": "暂停",
    "system": "系统",
    "tagged": "带标签",
    "target": "目标",
    "task": "任务",
    "terminal": "终态",
    "text": "文本",
    "thinking": "思考",
    "thread": "线程",
    "timeout": "超时",
    "timezone": "时区",
    "title": "标题",
    "to": "截止",
    "token": "令牌",
    "tokens": "令牌数",
    "tool": "工具",
    "tools": "工具",
    "topic": "主题",
    "total": "总计",
    "trace": "追踪",
    "transport": "传输",
    "trigger": "触发方式",
    "type": "类型",
    "unified": "统一格式",
    "until": "截止",
    "up": "后续",
    "updated": "更新",
    "url": "URL",
    "usage": "用量",
    "used": "使用",
    "user": "用户",
    "validated": "验证",
    "validation": "验证",
    "value": "值",
    "verifier": "校验器",
    "version": "版本",
    "vision": "视觉",
    "watermark": "水位",
    "window": "窗口",
    "worker": "工作节点",
    "workflow": "工作流",
    "workspace": "工作区",
}


def _parse_schema(schema_text: str) -> tuple[TableDefinition, ...]:
    tables: list[TableDefinition] = []
    active_name: str | None = None
    active_columns: list[str] = []

    for line_number, line in enumerate(schema_text.splitlines(), start=1):
        if active_name is None:
            match = _CREATE_TABLE_RE.fullmatch(line)
            if match is not None:
                active_name = match.group(1)
                active_columns = []
            continue

        if line.startswith(")"):
            if not active_columns:
                raise SchemaCommentError(f"table {active_name} has no columns")
            tables.append(TableDefinition(active_name, tuple(active_columns)))
            active_name = None
            active_columns = []
            continue

        match = _COLUMN_RE.match(line)
        if match is not None:
            column = match.group(1)
            if column in active_columns:
                raise SchemaCommentError(f"duplicate column {active_name}.{column} at line {line_number}")
            active_columns.append(column)

    if active_name is not None:
        raise SchemaCommentError(f"unterminated CREATE TABLE for {active_name}")

    table_names = [table.name for table in tables]
    if len(table_names) != len(set(table_names)):
        raise SchemaCommentError("duplicate static CREATE TABLE statement")
    column_count = sum(len(table.columns) for table in tables)
    if len(tables) != _EXPECTED_TABLE_COUNT or column_count != _EXPECTED_COLUMN_COUNT:
        raise SchemaCommentError(f"static schema shape changed: expected {_EXPECTED_TABLE_COUNT} tables/{_EXPECTED_COLUMN_COUNT} columns, found {len(tables)} tables/{column_count} columns; review comments and counts")

    actual = set(table_names)
    described = set(_TABLE_METADATA)
    if actual != described:
        missing = ", ".join(sorted(actual - described)) or "none"
        stale = ", ".join(sorted(described - actual)) or "none"
        raise SchemaCommentError(f"table metadata mismatch; missing descriptions: {missing}; stale descriptions: {stale}")

    actual_columns = {(table.name, column) for table in tables for column in table.columns}
    stale_overrides = set(_TABLE_COLUMN_PHRASES) - actual_columns
    required_overrides = {identity for identity in actual_columns if identity[1] in {"content", "first_human_message", "last_ai_message"}}
    missing_overrides = required_overrides - set(_TABLE_COLUMN_PHRASES)
    if stale_overrides or missing_overrides:
        missing = ", ".join(f"{table}.{column}" for table, column in sorted(missing_overrides)) or "none"
        stale = ", ".join(f"{table}.{column}" for table, column in sorted(stale_overrides)) or "none"
        raise SchemaCommentError(
            f"table-specific column metadata mismatch; missing descriptions: {missing}; stale descriptions: {stale}",
        )
    return tuple(tables)


def _humanize(identifier: str) -> str:
    words = identifier.split("_")
    unknown = [word for word in words if word not in _WORD_LABELS]
    if unknown:
        raise SchemaCommentError(f"column word glossary is incomplete for {identifier}: {', '.join(unknown)}")
    return "".join(_WORD_LABELS[word] for word in words)


def _column_phrase(table: str, column: str) -> str:
    table_specific = _TABLE_COLUMN_PHRASES.get((table, column))
    if table_specific is not None:
        return table_specific
    exact = _COLUMN_PHRASES.get(column)
    if exact is not None:
        return exact

    suffixes = (
        ("_id", "标识"),
        ("_at", "时间"),
        ("_count", "数量"),
        ("_version", "版本号"),
        ("_revision", "修订号"),
        ("_generation", "代次"),
        ("_checksum", "校验和"),
        ("_digest", "摘要"),
        ("_hash", "不可逆哈希"),
        ("_hmac", "HMAC 摘要"),
        ("_json", "JSON 数据"),
        ("_bytes", "字节数"),
        ("_seconds", "秒数"),
        ("_cursor", "游标"),
        ("_limit", "限额"),
        ("_number", "编号"),
        ("_num", "编号"),
        ("_order", "顺序"),
        ("_key", "键"),
        ("_ref", "引用"),
        ("_name", "名称"),
        ("_type", "类型"),
        ("_kind", "类型"),
        ("_role", "角色"),
        ("_status", "状态"),
    )
    for suffix, noun in suffixes:
        if column.endswith(suffix):
            stem = column[: -len(suffix)]
            return f"{_humanize(stem)}{noun}"

    if column.startswith("is_"):
        return f"是否{_humanize(column.removeprefix('is_'))}"
    if column.startswith("needs_"):
        return f"是否需要{_humanize(column.removeprefix('needs_'))}"
    if column.startswith("supports_"):
        return f"是否支持{_humanize(column.removeprefix('supports_'))}"
    return _humanize(column)


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _render(tables: tuple[TableDefinition, ...], *, schema_name: str) -> bytes:
    lines = [
        "-- Generated by backend/scripts/generate_schema_comments.py; DO NOT EDIT.",
        f"-- Source: {schema_name}",
        (f"-- Coverage: {_EXPECTED_TABLE_COUNT} static tables and {_EXPECTED_COLUMN_COUNT} columns."),
        "-- Comments describe schema purpose only; they contain no runtime or secret values.",
        "",
    ]
    for table in tables:
        label, description = _TABLE_METADATA[table.name]
        lines.append(f"COMMENT ON TABLE {table.name} IS {_sql_literal(description)};")
        for column in table.columns:
            phrase = _column_phrase(table.name, column)
            lines.append(f"COMMENT ON COLUMN {table.name}.{column} IS {_sql_literal(f'{label}：{phrase}。')};")
        lines.append("")
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise SchemaCommentError(f"refusing symbolic-link output: {path}")
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _embedded_schema(schema_text: str, comments: bytes) -> bytes:
    """Replace the checked-in COMMENT block without touching surrounding DDL."""

    start_count = schema_text.count(_BLOCK_START)
    end_count = schema_text.count(_BLOCK_END)
    if start_count != 1 or end_count != 1:
        raise SchemaCommentError(
            "full schema must contain exactly one generated-comment marker pair",
        )
    start = schema_text.index(_BLOCK_START)
    end = schema_text.index(_BLOCK_END, start) + len(_BLOCK_END)
    if schema_text.find(_BLOCK_END, 0, start) != -1:
        raise SchemaCommentError("generated-comment markers are out of order")
    rendered = comments.decode("utf-8").rstrip()
    block = f"{_BLOCK_START}\n{rendered}\n{_BLOCK_END}"
    return (schema_text[:start] + block + schema_text[end:]).encode("utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        type=Path,
        default=_SCHEMA_PATH,
        help="full schema SQL to inspect",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_OUTPUT_PATH,
        help="generated COMMENT ON SQL artifact",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="fail when the generated artifact is missing or stale",
    )
    mode.add_argument(
        "--stdout",
        action="store_true",
        help="write generated SQL to standard output without changing files",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.schema.is_symlink():
            raise SchemaCommentError(f"refusing symbolic-link schema: {args.schema}")
        schema_text = args.schema.read_text(encoding="utf-8")
        tables = _parse_schema(schema_text)
        expected = _render(tables, schema_name=args.schema.name)
        expected_schema = _embedded_schema(schema_text, expected)

        if args.stdout:
            sys.stdout.buffer.write(expected)
            return 0
        if args.check:
            if args.output.is_symlink() or not args.output.is_file():
                raise SchemaCommentError(f"generated artifact is missing: {args.output}")
            if args.output.read_bytes() != expected:
                raise SchemaCommentError(f"generated artifact is stale: {args.output}; rerun without --check")
            if args.schema.read_bytes() != expected_schema:
                raise SchemaCommentError(
                    f"generated comments in {args.schema} are stale; rerun without --check",
                )
            print(f"schema comments are current: {_EXPECTED_TABLE_COUNT} tables, {_EXPECTED_COLUMN_COUNT} columns")
            return 0

        _atomic_write(args.output, expected)
        _atomic_write(args.schema, expected_schema)
        print(
            f"generated {args.output} and updated {args.schema}: {_EXPECTED_TABLE_COUNT} tables, {_EXPECTED_COLUMN_COUNT} columns",
        )
        return 0
    except (OSError, UnicodeError, SchemaCommentError) as exc:
        print(f"error: schema comment generation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
