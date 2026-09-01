# 移除应用 Docker 构建与部署设计

> 状态：书面设计草案，待用户复核；本文不表示清理已经完成。
> 日期：2026-09-01。
> 仓库基线：`c3813bd2927a111896100b56aba3eef5641803cc`。
> 本文固化已确认的设计边界；复核后再编写实施计划并修改代码。

## 1. 目标

项目应用只保留本机进程运行方式，删除后端、前端和整套应用的 Docker 构建、Compose 部署、部署脚本及其活动文档。Nginx 继续作为本机浏览器入口，但配置移出 `docker/`。Docker 只作为可选的 Sandbox 运行时；Sandbox Provisioner 继续保留，但移动到独立的 Sandbox 目录，不再依赖应用 Compose。

目标运行拓扑如下：

```text
Browser -> Nginx :2026 -> Frontend :3000
                        -> Gateway :8001

Worker / Scheduler: host processes
Sandbox: optional local Docker containers
Provisioner: optional standalone Sandbox control service
```

## 2. 已确认事实、假设与非目标

### 2.1 已确认事实

- 当前仓库同时存在应用 Dockerfile、四份 Compose 文件、Compose 脚本、本机 Nginx 配置和 Sandbox Docker 支持。
- `scripts/serve.sh` 与 `scripts/nginx.sh` 使用 `docker/nginx/nginx.local.conf` 启动本机 Nginx。
- `AioSandboxProvider` 的 `runtime="docker"` 是本地 Sandbox 后端，属于保留范围。
- `docker/provisioner/` 只服务于 Sandbox/Kubernetes Provisioner，不是完整应用的部署目标。
- 当前 `compose_dood_p03_v1_verified` 允许在 Compose DooD 的不同宿主路径视图下绕过只读挂载安全检查；删除 Compose 后不应保留这项应用部署特例。

### 2.2 合理推测

- 删除旧命令会影响仍在使用 `make docker-*`、`make up/down` 或 `scripts/deploy.sh` 的本地操作习惯，因此活动安装文档必须同步给出本机启动命令。
- Sandbox 使用本地 Docker 时，Worker 与 Docker daemon 只有共享同一文件系统视图，才能安全提供 Run Skill 只读挂载。

### 2.3 未验证假设

- 没有外部自动化直接依赖仓库内已删除的 Compose 文件和脚本。仓库内引用会全部清理，但仓库外调用者需要自行迁移。
- 目标机器已经具备本机 Nginx、PostgreSQL、Python 和 Node.js 运行环境；本次不新增系统级安装器。

### 2.4 非目标

- 不删除 Docker Sandbox 后端，不把 Sandbox 改成其他容器实现。
- 不删除 `scripts/setup-sandbox.sh` 或 `scripts/cleanup-containers.sh`。
- 不取消 Kubernetes Sandbox Provisioner，也不重构其业务协议。
- 不设计新的生产部署平台、容器编排方案或远程发布流程。
- 不修改 RAG 文件解析功能、数据库业务逻辑或用户未授权的 M11 文档。

## 3. 文件边界

### 3.1 删除

| 类别 | 路径 |
| --- | --- |
| 应用镜像 | `.dockerignore`、`backend/Dockerfile`、`frontend/Dockerfile` |
| 应用 Compose | `docker/docker-compose.yaml`、`docker/docker-compose-dev.yaml`、`docker/docker-compose.cli-auth.yaml`、`docker/docker-compose.dood.yaml` |
| Compose 入口 | `docker/dev-entrypoint.sh`、`scripts/docker.sh`、`scripts/deploy.sh` |
| Compose Nginx | `docker/nginx/nginx.conf` |
| Compose DooD 验证 | `backend/tests/support/p03_compose_dood_probe.py`、`backend/tests/test_aio_run_skill_mount_lease_dood.py` |

完成移动后删除空的根目录 `docker/`。同时删除 Makefile 中应用 Docker/部署变量、帮助文本和目标，包括 `docker-init`、`docker-start`、`docker-stop`、Docker 日志、`up`、`down`。

### 3.2 移动

| 原路径 | 新路径 | 目的 |
| --- | --- | --- |
| `docker/nginx/nginx.local.conf` | `nginx/nginx.conf` | 明确这是本机应用入口配置 |
| `docker/provisioner/` | `sandbox/provisioner/` | 明确 Provisioner 只属于 Sandbox |

`sandbox/provisioner/Dockerfile` 保留，因为它只构建可选的 Sandbox 控制服务。其 README 和代码注释改为独立构建/运行或外部集群运行，不再引用 Compose。

### 3.3 保留

- `AioSandboxProvider` 的本地 Docker runtime 及对应有效测试。
- `scripts/setup-sandbox.sh` 和 `scripts/cleanup-containers.sh`。
- Sandbox Docker 镜像拉取、容器创建、回收和诊断能力。
- Provisioner 需要的 `ACT_WEAVE_HOST_BASE_DIR` 和路径映射能力；不得因为删除 Compose DooD 而误删 Kubernetes Provisioner 的路径契约。

## 4. Sandbox 安全边界

删除配置字段 `sandbox.compose_dood_p03_v1_verified`、示例配置、文档、探针和测试。Docker runtime 的只读挂载判定改为：

- `paths.run_skill_uses_distinct_host_view()` 为 `false` 时，允许按现有流程挂载。
- Docker runtime 且该值为 `true` 时，直接 fail closed，不再接受 attestation 或配置覆盖。
- 非 Docker 的 Provisioner 路径映射继续使用自身现有契约，不把本规则扩展成对所有 Sandbox 后端的禁用。

涉及的活动代码和测试至少包括：

- `backend/packages/harness/deerflow/community/aio_sandbox/aio_sandbox_provider.py`
- `backend/packages/harness/deerflow/config/sandbox_config.py`
- `backend/tests/test_app_config_access_paths.py`
- `backend/tests/test_aio_private_sandbox_lifecycle.py`
- `backend/pyproject.toml`
- `config.example.yaml`

该处理会收紧旧 Compose DooD 场景：路径视图不同的 Docker daemon 不能再通过手工声明绕过校验。它降低长期维护成本，也避免保留已经没有受支持部署入口的安全例外。

## 5. 脚本、配置和文档清理

### 5.1 本机运行入口

- `scripts/serve.sh` 和 `scripts/nginx.sh` 改用 `nginx/nginx.conf`。
- Makefile 保留本机开发/启动与 `setup-sandbox` 入口，删除应用 Docker 和 Compose 入口。
- 安装、检查、诊断和支持脚本只把 Docker 描述为可选 Sandbox 依赖，不再建议用 Docker 启动应用或 PostgreSQL。
- `scripts/detect_uv_extras.py` 删除 Docker build 语义；`scripts/doctor.py`、`scripts/check.py`、`scripts/support_bundle.py` 同步修正文案和判断。

### 5.2 文档所有权

- 根 `AGENTS.md` 将 `nginx/` 描述为本机 Nginx 配置，将 `sandbox/` 描述为可选 Sandbox Provisioner。
- 更新 `README.md`、`Install.md`、`backend/AGENTS.md`、`backend/CONTRIBUTING.md`。
- 更新活动的中英文部署指南、Sandbox 指南和介绍页面，删除应用 Docker/Compose 操作，保留 Docker Sandbox 使用说明。
- 清理活动测试和源码注释中的“Docker build/Compose deployment”表述，包括 `backend/tests/knowledge/test_knowledge_tokenizer.py`。
- 历史备份和已归档设计记录不作追溯改写，除非某个活动契约测试明确依赖它们。

## 6. 用户可见变化和迁移影响

清理后，不再提供以下入口：

- 后端或前端应用镜像构建。
- `docker compose` 启动整套应用。
- `make docker-*`、`make up`、`make down` 和 `scripts/deploy.sh`。

应用通过仓库现有本机安装及启动流程运行，Nginx 使用 `nginx/nginx.conf`。需要隔离执行环境时，仍可运行 `make setup-sandbox` 拉取/准备 Docker Sandbox；容器清理脚本继续可用。Provisioner 使用者改从 `sandbox/provisioner/` 独立构建或部署。

## 7. 验证策略

实施阶段使用聚焦验证，防止删除范围扩大：

1. 文件与引用检查：确认应用 Dockerfile、Compose、部署脚本和根 `docker/` 已消失，活动源码/文档不再引用旧路径或命令。
2. Nginx 检查：验证本机脚本均引用 `nginx/nginx.conf`，并运行现有适用的 Nginx 配置/脚本测试。
3. Sandbox 检查：验证 Docker Sandbox、`setup-sandbox.sh`、`cleanup-containers.sh` 和 `sandbox/provisioner/Dockerfile` 仍存在。
4. 安全契约测试：验证 Docker runtime 在不同宿主路径视图下 fail closed，在共享路径视图下保持原有行为。
5. 模块检查：运行受影响的后端格式化、聚焦单元测试、脚本测试和文档引用检查；不把这些结果表述为真实外部部署或 Kubernetes 集群验证。
6. 工作树检查：提交只包含本设计和随后批准的实施变更，不触碰主工作树中的 M11 用户修改。

## 8. 长期维护影响

该边界减少三套重叠的应用运行方式：仓库只维护本机应用启动和一个 Nginx 配置。Docker 相关维护集中在 Sandbox，Provisioner 也拥有独立目录。代价是项目不再提供可复制的应用容器部署路径；未来若重新支持容器化应用，应作为新的部署方案单独设计，不能把 Sandbox Dockerfile 或 Provisioner 当成完整应用镜像复用。
