# IM Channel Connections

ActWeave supports project-admin-managed IM channel instances for Telegram, Slack, Discord, Feishu/Lark, DingTalk, WeChat, and WeCom. Each project can own one current instance of each provider. A project may bind a group to one Agent; the backend model is provider-neutral, while the current group-binding UI and adapter flow support Feishu only. Owner-attributed connection rows remain a runtime routing primitive, not a member-facing personal configuration surface.

No public IP, OAuth callback URL, or provider webhook is required in this implementation.

## Configuration

Project Admins configure providers on `/projects/{project_slug}/connections`. Public application identifiers remain project metadata; every Secret is accepted as a write-only value, encrypted as an exact project Credential version, and never returned by the API. Saving a changed Secret rotates the Credential version. Leaving an already configured Secret field blank preserves the current version.

Database-backed project instances are exposed from their enabled, configured,
and running state. They do not require `channel_connections.*` in
`config.yaml`.

`channel_connections.*` remains only for the deployment-config compatibility
path. When a project has no database-backed instance for a provider, an
operator may expose the corresponding legacy `channels.*` provider with a
nullable `channel_instance_id`:

```yaml
channel_connections:
  enabled: true

  telegram:
    enabled: true
    bot_username: $TELEGRAM_BOT_USERNAME

  slack:
    enabled: true

  discord:
    enabled: true

  feishu:
    enabled: true

  dingtalk:
    enabled: true

  wechat:
    enabled: true

  wecom:
    enabled: true
```

`channel_connections` does not store provider Secrets and does not control
database-backed project instances. In the compatibility path, Telegram may
include `bot_username` so the frontend can open a deep link.

The project instance API is:

- `GET /api/projects/{project_id}/channel-instances`
- `PUT /api/projects/{project_id}/channel-instances/{provider}`
- `POST /api/projects/{project_id}/channel-instances/{provider}/enable`
- `POST /api/projects/{project_id}/channel-instances/{provider}/disable`
- `DELETE /api/projects/{project_id}/channel-instances/{provider}`

Only a project Admin with `project.channels.manage` may read bounded status or configure, rotate, enable, disable, or delete an instance. Existing deployment-level `channels.*` entries remain an explicit compatibility fallback only when a project has no database-backed instance for that provider; new UI configuration never writes provider Secrets to `config.yaml` or a local plaintext runtime file.

Every executable inbound message requires a connected PostgreSQL row and the
project inbound dispatcher. `channel_connections.*` affects only discovery of
legacy deployment-config providers; it neither grants execution authority nor
hides or disables an exact database-backed project instance. Missing
persistence/runtime dependencies and unbound, frozen, or revoked connections
fail closed before a Thread Run is admitted. There is no legacy open-bot or
auth-disabled execution bypass.

## Owner-attributed Connect Compatibility Flow

The project UI does not expose a member-facing personal connection control. An
authenticated project Admin may still begin an owner-attributed compatibility
binding with
`POST /api/projects/{project_id}/connections/{provider}/connect`, supplying the
selected Agent asset reference. The response contains the one-time code (and a
Telegram deep link when applicable). List and disconnect use
`GET /api/projects/{project_id}/connections` and
`DELETE /api/projects/{project_id}/connections/{connection_id}`.
Every endpoint and callback revalidation requires `project.channels.manage`.
The persisted owner remains an exact inbound private-work routing coordinate;
it does not make this a member configuration page.

Telegram:

- The Admin-only compatibility API creates a short one-time code.
- An authorized administrative client may open
  `https://t.me/<bot_username>?start=<code>`.
- The existing Telegram long-polling worker receives `/start <code>` and binds that Telegram chat/user to the selected ActWeave project and owner.

Slack:

- The Admin-only compatibility API creates a short one-time code.
- An authorized administrative client may instruct the Admin to send
  `/connect <code>` to the ActWeave Slack bot.
- The existing Slack Socket Mode worker receives the message and binds the Slack user/team to the selected ActWeave project and owner.

Discord:

- The Admin-only compatibility API creates a short one-time code.
- An authorized administrative client may instruct the Admin to send
  `/connect <code>` to the ActWeave Discord bot.
- The existing Discord Gateway worker receives the message and binds the Discord user/guild to the selected ActWeave project and owner.

Feishu/Lark, DingTalk, WeChat, and WeCom:

- The Admin-only compatibility API creates a short one-time code.
- An authorized administrative client may instruct the Admin to send
  `/connect <code>` to the ActWeave provider bot.
- The already-running long-connection or polling worker receives the message and binds the platform identity to the selected ActWeave project and owner.

Codes use 128 bits of randomness, expire after 10 minutes, and are single-use.

For providers with an `allowed_users` allowlist (Telegram, Slack, DingTalk, WeChat, …), a valid `/connect <code>` (or Telegram `/start <code>`) is consumed **before** the allowlist is checked. This is intentional: a user who is not yet on the allowlist — and whose platform identity the bot has therefore never seen — can still complete their first browser-initiated bind. After binding, `allowed_users` continues to gate ordinary (non-bind) messages as before.

## Project Group Bind Flow

Feishu 群绑定由具有 `project.channels.manage` 权限的项目 Admin 发起：

1. Admin 在项目“渠道连接”中选择一个当前可用的 Agent，生成有效期 10 分钟的一次性命令。
2. Admin 在目标飞书群发送 `/bind-project <code>`。长连接 adapter 消费该命令，将当前群、项目渠道实例和 Agent 原子绑定。
3. UI 检查绑定结果；成功后只显示群名、Agent、启用状态和最近活动时间。Admin 可修改 Agent、启用、停用或删除该绑定。

绑定后，群成员直接向机器人发送消息，无需 ActWeave 账号，也无需执行个人 `/connect`。每个飞书 sender 映射为项目内独立的伪名 `channel_guest` owner；同一 sender 在同群同话题中复用自己的 Thread，不同 sender 即使回复同一话题也使用不同 owner、Thread、Memory、文件和 Run 范围。群聊不形成共享上下文。

`channel_guest` 不可登录，不进入公开成员列表、人类账号计数或成员配额对账；其 Thread 也不出现在已登录成员的普通网页会话菜单。Admin 的群绑定列表不返回聊天正文、Thread/Run 内容或原始平台标识。带 owner 归属的 `p2p /connect` 兼容流程也仅限项目 Admin，且不作为成员个人配置入口暴露。

The project group-binding API is:

- `GET /api/projects/{project_id}/channel-group-bindings`
- `POST /api/projects/{project_id}/channel-group-bindings/challenge`
- `PATCH /api/projects/{project_id}/channel-group-bindings/{binding_id}`
- `DELETE /api/projects/{project_id}/channel-group-bindings/{binding_id}`

## Runtime Model

Connection records live in SQL tables under `deerflow.persistence.channel_connections`:

- `project_channel_instances`: project/provider desired configuration and bounded observed status.
- `project_channel_credential_bindings`: the exact active project Credential version for one instance.
- `project_channel_instance_leases`: single-writer lease and monotonically increasing fencing generation.
- `channel_connections`: project, owner user, exact channel instance, provider identity, workspace/guild/team, status, and server-owned Agent metadata.
- `channel_oauth_states`: project, owner, exact channel instance, one-time connect codes, and Telegram deep-link state.
- `channel_conversations`: project-and-owner-scoped IM conversation to private ActWeave Thread mapping.
- `channel_credentials`: reserved for future provider-token flows, not used by the local/private binding flow.
- `project_channel_group_binding_challenges`: short-lived, single-use Admin group-binding challenges; only the command digest is persisted.
- `project_channel_group_bindings`: exact project/instance/provider group binding, selected Agent, lifecycle state, and bounded activity timestamps.
- `channel_external_principals`: per-binding pseudonymous sender to isolated `channel_guest` owner mapping.

For ordinary bound text, `(channel_instance_id, provider identity)` is the runtime routing coordinate. The persisted external identity lookup is the only source of project and owner. The resolver rechecks active membership, creates or reuses a private Thread, and launches the existing Gateway run lifecycle through `start_private_run`. The raw platform user id remains the runtime-only `channel_user_id`.

Every enabled project instance materializes one provider adapter. Multiple projects can therefore run multiple Feishu, Slack, or other provider adapters in the same Gateway process, each with isolated configuration and exact instance routing. A PostgreSQL lease ensures that only one Gateway process owns an instance at a time. Lease generation fences status writes, inbound admission, binding commands, and outbound delivery; a non-owner monitors the instance and takes over after the lease expires. Configuration or Credential closure changes trigger a fenced restart on the current owner.

Changing a public provider application identity freezes existing member connections because those external identities must bind to the new application. Rotating only a Secret keeps the application identity and member bindings. Disable stops the runtime without deleting configuration. Delete stops the runtime, revokes its Credential binding and internal Credential, soft-deletes the instance, and freezes its member connections.

Group, sender, and topic/response-alias identifiers are persisted only as domain-separated HMAC references. Retained-key rotation keeps every alias on the exact HMAC generation selected by the bound group. Concurrent first messages derive the same deterministic private Thread ID and converge through database uniqueness without holding an extra advisory-lock connection, including when the configured pool has one connection and no overflow. Disabling or deleting a group binding freezes all derived guest principals and their connection rows before later inbound admission. Project physical retention removes group challenges, bindings, external principals, guest connections/conversations, and guest-owned private data; it deletes only unreferenced `channel_guest` memberships/users and never a human account or retained governance reference.

Nullable `channel_instance_id` rows exist only for the explicit deployment-config compatibility path. All new project UI/API flows require an exact non-null instance id.

## Security Notes

- Browser APIs remain authenticated and CSRF-protected.
- Provider Secret submissions use imperative authenticated requests; they are never placed in TanStack Query/Mutation cache, response bodies, project metadata, or logs. Request-schema failures use a stable error envelope and do not echo rejected secret-bearing input.
- Provider public configuration is allowlisted per provider. Feishu/Lark domain overrides accept only official exact HTTPS origins, preventing a project Admin from turning the Gateway into an SSRF client.
- Provider Credentials are decrypted only from an exact active instance binding, Credential, version, and envelope closure inside Gateway memory.
- Runtime status is bounded to stable codes. Raw SDK/provider errors and Credential material are not returned by project APIs.
- Lease ownership is revalidated before account binding, executable inbound admission, and outbound delivery. Loss of persistence or lease authority fails closed and stops or fences the stale adapter.
- Connect codes are 128-bit random, short-lived, and single-use.
- Stored per-connection credentials use the `channel_credentials` encryption
  path. If stored credential material cannot be decrypted, ActWeave treats it
  as unavailable instead of using corrupt secrets.
- `allowed_users` is **not** a bind-time defense. Because connect codes are processed before the allowlist (see Owner-attributed Connect Compatibility Flow), anyone who possesses a valid code can consume it — not only allowlisted users. Bind security therefore rests entirely on the code's confidentiality: it is 128-bit random, expires after 10 minutes, and is single-use. An authorized administrative client must treat the code like a one-time password, avoid persistence or logs, and never forward it.
- An external identity — `(provider, external account, workspace/team/guild)` — has at most one active owner. The most recent successful bind wins: connecting an identity that another ActWeave user already holds transfers ownership and revokes the previous owner's binding (and its stored credentials). This is enforced at the database layer, so two users racing to bind the same identity cannot both end up connected.
- Deployment-level compatibility provider tokens may remain in `channels.*`; project UI configuration stores its Secrets only in encrypted project Credentials.
- This implementation does not add public provider callback or webhook routes.
