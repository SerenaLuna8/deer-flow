# Project-scoped 路径示例

## HTTP resources

```text
/api/projects/{project_id}/private-work/threads
/api/projects/{project_id}/private-work/threads/{thread_id}/runs
/api/projects/{project_id}/private-work/threads/{thread_id}/uploads
/api/projects/{project_id}/private-work/threads/{thread_id}/files/{file_id}
/api/projects/{project_id}/private-work/artifacts/{artifact_id}
/api/projects/{project_id}/memory
/api/projects/{project_id}/automations
```

这些路径中的 `project_id` 只用于定位；真正 authority 来自认证身份和服务端解析的 ProjectContext。repository 仍必须同时过滤 owner。

## Sandbox paths

```text
/mnt/user-data/workspace
/mnt/user-data/uploads
/mnt/user-data/outputs
```

虚拟路径只能在 admitted Run 的 sandbox 内解析。API 不接受宿主机绝对路径，也不把宿主机路径返回给浏览器。

## Local configuration

```text
config.yaml
.env
```

真实配置文件位于仓库根目录并被 gitignore。项目资产、Memory 和 Connection 不使用本地 JSON 文件作为生产权威。
