# 模块 10 Auth/Authz 验收证据

验收日期：2026-07-30

本目录只保存脱敏后的浏览器证据和验收摘要，不保存账号标识、密码、JWT、CSRF、邀请
token、session id、数据库连接串或 Cookie 内容。工作空间和对话截图在浏览器截图时直接
裁掉账号区域，未对业务结果区域做生成式修改。

## 截图

1. `01-uppercase-login-workspace-redacted.jpg`
   - 浏览器使用大小写变体邮箱登录成功后进入的工作空间；
   - 默认项目正常可见；
   - 截图已裁掉账号标识，大小写输入动作由同一次浏览器操作记录交叉证明。
2. `02-real-model-context-after-restart-redacted.jpg`
   - 同一项目会话连续完成四轮真实外部模型调用；
   - 第三轮正确读取前两轮结果；
   - 完整服务重启后，第四轮仍能读取第三轮上下文并返回
     `RESTART-CONTEXT-PASS | prior=CONTEXT-CHAIN-PASS`。
   - 截图已裁掉侧栏账号标识，保留第三、四轮结果与 token 统计。
3. `03-login-remember-ui.jpg`
   - 不含账号值的真实登录页；
   - 显示 `remember_me` 选择和普通注册入口；
   - 密码框只显示固定掩码/placeholder，不含密码值。

## 真实浏览器与 HTTP/数据库交叉验收

按顺序完成：

- 新空 PostgreSQL 数据库执行 `make setup-db`，再通过浏览器创建首个管理员和默认项目；
- 退出后访问带 query 的私有工作空间，确认跳转登录并保留安全返回路径；
- 使用大小写变体邮箱登录，确认命中同一小写规范账号；
- 使用大小写变体重复注册，确认显示稳定的“邮箱已注册”错误；
- 临时关闭 `auth.local.allow_registration`，确认登录页不显示注册入口，直接注册 API 返回
  `403 registration_disabled`；随后恢复配置并重启；
- 新账号在未加入项目时，浏览器显示项目不可用，直接项目请求返回
  `404 PROJECT_NOT_FOUND`；
- 通过项目邀请加入 Viewer 后，浏览器不显示项目管理入口；Viewer 修改项目返回
  `403 PROJECT_FORBIDDEN`；
- 在同一 Viewer 请求上伪造项目角色、capability 和 system role 请求头，结果仍为
  `403 PROJECT_FORBIDDEN`；
- 精确撤销该测试账号的 durable session 后，现有浏览器访问私有路由被送回登录页；
- 管理员账号在同一会话完成四轮真实 DeepSeek V4 Pro 调用，第三轮验证多轮上下文，
  完整服务重启后第四轮验证持久上下文；
- PostgreSQL 交叉检查四个 Run 均为 `success`，finalization 均为 `complete`，job 均为
  `succeeded`，每轮 `llm_call_count=1`、`stream.end=1`。

## 自动化门禁

- 后端完整测试：`7400 passed, 1012 skipped`；
- 固定 20 文件 M1–M7 真实 PostgreSQL gate：`270 passed, 0 failed, 0 skipped`；
- 模块 10 新增真实 PostgreSQL 聚焦测试：`7 passed, 152 deselected`；
- Auth 独立复审集合：`220 passed`；核心组合 `194 passed`；非 PostgreSQL 集合
  `94 passed`；
- 前端完整单元测试：`188 files, 1344 passed`；
- `pnpm check` 通过；
- `pnpm build` 通过，静态页面 `78/78`；
- `make check-db` 通过，schema marker 为 `full_schema_v1`。

## 结论

模块 10 的 canonical email、本地注册开关、remember-me Cookie 策略、durable session、
项目 404/403 权限边界、客户端 authority 防伪造和真实 Run 链路均完成验证。独立后端与
前端代码复审未发现剩余 P0/P1/P2 阻塞项。原始未裁切截图因账号 local-part 与测试密码相同
而被删除；本目录只保留重新截取并完成视觉核验的脱敏证据。
