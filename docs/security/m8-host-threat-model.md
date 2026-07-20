# M8 宿主机发布威胁模型

本文件固定 M8 对 final M7 架构的安全复核边界。权威的机器可读映射位于
`contracts/m8_threat_controls.json`；每个威胁族必须同时绑定 prevention control、detective control、
isolation matrix case 和可执行测试 selector，缺少任一项即阻断发布。

## 信任边界

- Gateway 只接受 server-issued account、project、membership 和 owner authority；Gateway 不执行 Agent graph。
- Worker 是唯一 graph executor，并在 job lease、checkpoint、stream、tool、file 等副作用边界重新验证 authority。
- Scheduler 只持有 PostgreSQL singleton ownership 并原子 admission，不执行 graph。
- PostgreSQL 是业务数据、配额、job、stream、audit 与 recovery proof 的唯一持久化权威；应用不使用 RLS。
- DeepSeek、IM provider、MCP server 和 sandbox provider 都是外部边界，secret 只在最小调用窗口内物化。
- 本机进程、临时路径、端口、测试数据库和 release evidence 必须由单次 acceptance invocation 精确拥有。

## 阻断条件

- isolation matrix 出现未覆盖或孤立 surface；
- dependency audit 对 resolved production graph 产生有效 advisory；
- tracked tree、review diff、Git history、evidence、support bundle 或新增 runtime log 范围出现未登记 secret；
- secret allowlist 使用 wildcard、非精确路径、非精确 detector rule 或非精确值 digest；
- audit、support bundle 或失败 evidence 包含 raw exception、credential、URL userinfo、cookie、private identifier 或正文；
- recovery archive、journal、source identity、restore target 或 proof 任一绑定不一致。

## 响应原则

发现真实或无法证明为假的 credential 时，立即停止外部调用，在仓库外轮换 credential，并保留只含 rule ID 与
位置 digest 的证据。M8 runner 不自动改写 Git history。对进程、文件或数据库 ownership 无法复核时保持原状并
报告 quarantine-required failure，不执行 broad kill、glob delete 或不受约束的 DROP。

## 威胁族

机器合同固定九类威胁：scoped identifier authority、stale runtime authority、Credential secret containment、
web session boundary、file/archive/sandbox boundary、durable stream boundary、Automation/Scheduler boundary、
recovery integrity，以及 system governance/observability。具体攻击面、控制、证据和 operator response 以机器合同为准。
