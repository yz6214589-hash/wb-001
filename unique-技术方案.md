# unique 技术方案

| 条目 | 内容 |
|---|---|
| 文档类型 | 技术方案（研发开工前置设计文档） |
| 文档版本 | v0.1 |
| 编写日期 | 2026-11-11 |
| 写作依据 | [unique-业务规格说明书.md](unique-业务规格说明书.md) v0.6.1 第 3、4、7、8、10 章；PR #14、#15、#16 均已合并 |
| 读者 | 后端、前端、Runner 客户端、测试、运维 |
| 定位 | 业务规格书不定义技术架构，本文补协议、数据模型、部署与适配实现 |

> 口径约定：业务规则以业务规格说明书 v0.6.1 为准，本文只定义实现方式，不新增业务概念。规格书与本文冲突时，先确认哪一份是准的；要改业务规则，先改规格书再同步本文。PR #14、#15、#16 已合并进 main，本文按合并后的口径撰写。

## 0. 结论摘要

### 0.1 本文回答的问题

1. Runner 与中央服务之间怎么通信，断网、过期、重复上报分别怎么处理。
2. 业务规格第 8 章的对象落到数据库是什么表、什么字段、什么约束。
3. 服务端有哪些组件，怎么部署，网络怎么开，数据怎么备份。
4. 三款执行 CLI 与 Trae 的差异在哪里，执行适配器怎么写，Trae 为什么不能作为执行入口，缺能力时怎么降级。
5. 一条任务从排队到 PR、从离线到改派、从失败到重试、从缺陷到 Issue 同步，时序上分别发生什么。
6. 哪些技术选择还没定，哪些口径要业务方确认。

### 0.2 技术基线

下表是建议基线，用于统一研发开工时的默认选择；尚未锁定的事项统一列在第 7 章。

| 领域 | 基线选择 | 理由 |
|---|---|---|
| 服务端语言与形态 | TypeScript + Node.js 22，模块化单体，单进程内按模块隔离，调度与连接器以独立 worker 进程运行 | 10 人团队、100 并行 Run 的规模不需要微服务；前后端同语言，CLI 子进程与流式解析生态成熟 |
| HTTP 框架与接口风格 | Fastify + REST/JSON，OpenAPI 3.1 契约先行 | 接口面窄、调试成本低；契约可生成 Runner 客户端类型 |
| 数据库 | PostgreSQL 16，单主一从 | 租约、队列状态、幂等键都要求强一致与事务；JSONB 足以承载快照与配置 |
| 队列与缓存 | Redis 7（Redis Streams 做领取队列加速），PostgreSQL 为唯一事实来源 | Redis 丢失后可从 PostgreSQL 重建队列，不产生第二套事实 |
| 对象存储 | S3 兼容对象存储（自建可用 MinIO） | 日志分片、附件、非代码产出物体量大，不放进数据库 |
| 前端 | React 18 + TypeScript + Vite，PC 与移动端同一应用按断点适配 | 移动端只读是响应式页面，不做独立 App |
| Runner | Node.js 22 打包为单文件可执行程序，Windows 与 macOS 各一份安装包 | 与适配器同一运行时；避免要求成员预装开发环境 |
| Runner 本地缓存 | 文件型 spool 目录 + 嵌入式 SQLite 索引 | 断网 7 天缓存与断点续传，不依赖本地数据库服务 |
| 认证 | Web 用 GitHub OAuth 会话；Runner 用设备令牌；仓库写操作用 GitHub App 短时安装令牌 | 与规格第 10 章「复用 GitHub 授权」一致，长期模型凭证只留在成员本机 |
| 可观测性 | 结构化日志 + Prometheus 指标 + 站内与邮件告警 | 对应规格 10.5 的运行指标；不引入外部 SaaS 依赖 |

### 0.3 与规格书的章节对应

| 本文内容 | 规格书依据 |
|---|---|
| Runner 注册、心跳、租约、幂等键、旧 Runner 只读、兼容窗口、断网重连 | 4.3、4.4、FR-06、FR-07、8.6、8.14 |
| 数据模型 | 规格第 8 章全部对象（8.1–8.26） |
| 部署拓扑、网络、存储、备份、环境 | 规格 10.1–10.6、4.1 |
| CLI 适配器与降级 | 规格 FR-16、FR-20、7.3、第 13 章风险表 |
| 关键时序 | 规格 4.2、5.1、5.3、5.6、7.4、7.5 |
| 风险与未决项 | 规格第 13 章、14.2 中尚未闭环的项 |

### 0.4 本文不覆盖

- 不定义产品交互与页面视觉，页面以原型与规格第 9 章为准。
- 不引入自动部署；流水线终点仍是分支 + PR。
- 不设计多租户、计费与对外开放接口。
- 不写代码、不建构建脚本、不引第三方依赖清单；技术选型的最终版本在开工评审时确认。

## 1. 总体架构

### 1.1 组件关系

```
+-----------------------------+          +------------------------------+
| GitHub                      |          | 成员电脑                      |
|  仓库 / PR / Issue / Webhook |          |  Runner（单文件程序）          |
+--------------+--------------+          |   +----------------------+   |
               | webhook / API           |   | 执行适配器：Codex    |   |
               v                         |   | 执行适配器：Claude   |   |
+-----------------------------+          |   | 执行适配器：OpenCode |   |
| 中央 Web 服务（模块化单体）   |<-------->|   +----------+-----------+   |
|  Web/API 与权限校验          |  HTTPS   |              | 子进程 stdio   |
|  调度中心（队列/租约/重试）   |  出站     |              v               |
|  Runner 网关（注册/心跳/上报）|          |        本地 CLI 进程          |
|  连接器（GitHub/飞书/邮件）   |          |  工作目录 / spool 缓存        |
|  日志与产出物管道             |          |                              |
+------+--------+-------------+          |  Trae IDE（人工触发，P2）      |
       |        |                       |  Hook 回传事件，无执行入口     |
       v        v                       +------------------------------+
 PostgreSQL   Redis + 对象存储
```

关键约束：Runner 不开放公网入站端口，所有通信由 Runner 主动发起；中央服务不向 Runner 下发可执行脚本，只下发声明式配置与指令。

### 1.2 服务端模块划分

| 模块 | 职责 | 运行形态 |
|---|---|---|
| 身份与权限 | GitHub OAuth 登录、账号开通、成员、产品成员关系、权限点校验 | Web 进程内模块 |
| 产品与工作项 | 产品、版本、需求、任务、子任务、验收标准、依赖 | Web 进程内模块 |
| 调度中心 | 队列排序、能力匹配、并发配额、租约发放与回收、改派、重试、超时 | 独立 worker 进程，单实例选主 |
| Runner 网关 | 注册、心跳、领取、续约、状态与日志分片接收、指令下发 | Web 进程内模块，无状态可多实例 |
| 执行记录 | Run、会话、上下文快照、产出物、成本 | Web 进程内模块 |
| 连接器 | GitHub webhook 与 API、飞书文档同步、邮件发送 | 独立 worker，按连接器隔离重试 |
| 日志与产出物管道 | 分片校验、对象存储写入、归档、检索索引 | 独立 worker |
| 通知与审计 | 站内与邮件通知、审计写入、导出 | Web 进程内模块 + worker |

调度中心选主使用 PostgreSQL 咨询锁，保证同一时刻只有一个调度实例发放租约；多实例只做热备，不做分片，避免租约归属出现双主。

### 1.3 职责边界

- **中央服务**：唯一事实来源，维护所有业务状态、租约、幂等记录、权限与审计。
- **Runner**：只执行与采集。不判断业务状态，不自行重试业务动作，不创建 PR（由服务端创建），不修改任务状态。
- **适配器**：只做工具差异翻译，包括启动参数、事件解析、会话恢复、用量解析、Agent 配置落盘。
- **GitHub**：代码与评审状态的权威来源；缺陷业务字段以平台为准，Issue 状态与讨论以 GitHub 为准。

### 1.4 关键设计决策

| 决策 | 选择 | 放弃的方案与原因 |
|---|---|---|
| 租约存哪里 | PostgreSQL 行锁 + 唯一约束，Redis 只做加速 | 只用 Redis：故障后租约状态丢失，无法判断旧 Runner 是否还有权执行 |
| Runner 如何接收指令 | 心跳响应携带指令队列，Runner 确认后删除 | WebSocket 长连接：断网重连与多实例路由复杂，收益不足 |
| PR 由谁创建 | M1/M2 由平台用 GitHub App 创建，Runner 只推分支；M3/M4 由人创建 | Runner 直接建 PR：正文模板、幂等、权限收敛都难统一 |
| 仓库写权限如何给 | 服务端按租约签发短时安装令牌，随心跳下发 | 成员长期 OAuth 令牌写仓库：旧 Runner 无法在凭据层被限制 |
| 日志如何传 | 1 MB 分片、按序号幂等、对象存储落盘 | 整段日志一次性上传：断网重传成本高，大日志会阻塞 |
| 状态如何写 | 所有状态变更走单一状态机服务，写状态前校验租约与当前状态 | 各模块直接改表：派生状态与审计会不一致 |

## 2. Runner 通信协议

### 2.1 设计前提

1. **单向出站**：Runner 不开放入站端口，注册、心跳、领取、上报、拉指令全部由 Runner 发起 HTTPS 请求。
2. **服务端权威时间**：租约到期、超时、离线判定一律以服务端时间为准；响应携带 `server_time`，Runner 用于校正本地时钟偏移并展示。
3. **至少一次投递 + 幂等**：网络重试不保证只到达一次，所有写操作都必须带幂等键，重复提交返回首次结果。
4. **单一状态写入口**：Run、子任务、任务的状态只由服务端状态机服务写入，Runner 只能「上报发生了什么」，不能指定目标状态。
5. **协议版本化**：路径前缀 `/api/runner/v1`，请求头带 `X-Runner-Version`、`X-Protocol-Version`、`X-Request-Id`。

### 2.2 注册与设备绑定

`POST /api/runner/v1/register`

| 字段 | 类型 | 说明 |
|---|---|---|
| device_fingerprint | string | 设备指纹，安装时生成并持久化 |
| name / os / arch | string | 设备名、操作系统、架构 |
| agent_version | string | Runner 版本，语义化版本号 |
| protocol_version | integer | 协议版本，当前为 1 |
| adapters | array | 已安装适配器及版本、能力标志 |
| model_accounts | array | 可用模型账号的脱敏引用，不上传凭证本体 |
| max_concurrency | integer | 本机并发上限，成员自行配置 |
| workspace_root_hash | string | 工作目录根路径的哈希，用于隔离核对 |

响应返回 `runner_id`、`runner_token`（只返回一次）、`server_time`、`min_supported_version`、`config_version`。

规则：

- 首次绑定必须在网页登录态下确认，确认后才签发设备令牌；令牌存操作系统钥匙串，不写入配置文件明文。
- 注册幂等键为「设备指纹 + 账号」，重复注册返回原 `runner_id` 并轮换令牌，旧令牌立即失效。
- 账号停用或设备丢失时吊销令牌，下一次心跳即失败，Runner 进入只读并提示重新注册。

### 2.3 心跳与续约

`POST /api/runner/v1/heartbeat`，默认每 30 秒一次。

请求携带 `runner_id`、`agent_version`、运行状态、活跃租约列表（`lease_id`、`run_id`、最近事件序号）、已确认指令序号、spool 占用与本地时钟偏移。

响应携带 `server_time`、每个租约的续约结果与新的到期时间、待执行指令、是否暂停领取、配置版本。

规则：

- 心跳同时完成续约，续约必须携带 `run_id` 与 `lease_id`，不允许跨 Run 复用租约。
- 续约成功把到期时间顺延为「服务端当前时间 + 90 秒」，不做累加。
- 连续 2 个心跳周期（60 秒）未收到心跳判定离线；离线不立即失败 Run，按租约规则处理。
- 若租约已被服务端回收，响应返回 `granted=false` 与原因；Runner 立即停止本地执行工具、转为只读，只补传日志。
- 心跳本身幂等，重复到达只刷新最近心跳时间与租约到期时间。

### 2.4 任务领取与租约

`POST /api/runner/v1/leases/claim`，幂等键 `claim:{subtask_id}:{run_seq}`。

服务端在单个事务内完成：

1. 按「优先级降序、进入排队时间升序」选取候选子任务，使用 `FOR UPDATE SKIP LOCKED` 避免多 Runner 抢占同一条记录。
2. 校验成员与 Runner 绑定、适配器匹配、依赖完成、仓库权限、外发开关、Runner 版本、本地与全局并发余量。
3. 创建或复用 Run（`dispatching` 状态），创建租约行，返回租约令牌与执行载荷。
4. 同一子任务同一时刻最多一个活跃（非终态）Run；双跑显式成对、属于同一双跑组。单跑由数据库部分唯一约束兜底。

领取成功返回：

| 字段 | 说明 |
|---|---|
| lease_id / lease_token | 租约编号与一次性令牌，令牌只返回一次，服务端只存哈希 |
| run_id / subtask_id / run_seq | 本次执行标识与序号 |
| expires_at | 租约到期时间，服务端时间 |
| execution_timeout_at | 执行超时时刻，默认领取后 120 分钟 |
| payload | 上下文快照引用、验收标准版本、适配器与 Agent 配置、仓库与版本分支、执行模式 |

规则：

- 租约有效期 90 秒（3 个心跳周期），由心跳续约；不存在「永久锁」。
- 领取响应丢失时，Runner 用同一幂等键重试；服务端在 10 分钟内返回加密缓存的首次响应（含令牌），超过 10 分钟返回冲突，由 Runner 放弃本次领取。
- 主动释放调用 `DELETE /leases/{lease_id}`，释放原因写入审计；释放后 Run 置「已中断」，按重试规则生成新 Run。
- 租约到期由回收器处理，每 10 秒扫描一次：Run 置「已中断（租约过期）」，子任务回到「排队中」，生成新 Run，序号递增。原 Run 不复活，新 Run 不复用原记录。

领取失败必须返回具体原因码，不允许静默失败：缺少适配器、仓库无权限、依赖未完成、并发配额不足、成员无执行权限、外发未开启、Runner 版本不支持。

### 2.5 指令下发

指令由服务端产生，Runner 在心跳响应中按序号拉取，执行后在下一次心跳确认，未确认的指令按 TTL 重投。

| 指令 | 用途 | 过期时间 |
|---|---|---|
| `g4_reply` | 把人的回复送入当前会话 | 24 小时或 Run 终结 |
| `cancel_run` | 终止当前 Run | 10 分钟 |
| `pause_claim` / `resume_claim` | 暂停或恢复领取新任务 | 24 小时 |
| `rotate_credential` | 轮换仓库短时令牌或设备令牌 | 10 分钟 |
| `config_update` | 刷新适配器与 Agent 声明式配置 | 24 小时 |
| `request_diagnostics` | 触发本地环境自检并上报结果 | 1 小时 |

规则：

- `cancel_run` 到达后，Runner 终止进程树（Windows 使用作业对象或 `taskkill /T`，macOS 使用进程组信号），补传最终日志并上报「已取消」。
- `g4_reply` 到达时若原 Run 已结束，服务端先生成新 Run 再投递回复；Runner 不得把回复写入已结束的会话。
- 指令不携带可执行脚本，只携带结构化参数；Runner 拒绝执行未知指令类型。

### 2.6 Run 状态上报

`POST /api/runner/v1/runs/{run_id}/state`，幂等键 `{run_id}:{state_seq}`。

请求携带 `lease_id`、`lease_token`、`state_seq`、事件类型（开始执行、产出完成、失败、中断、取消）、错误分类、退出码、摘要与时间戳。

服务端校验顺序：令牌归属 → 租约是否有效 → 序号是否递增 → 状态迁移是否合法。任一不通过返回明确错误码，不写状态。

- Runner 只上报「执行中 / 已产出 / 失败 / 已中断 / 已取消」，不上报「超时」；超时由服务端在 `execution_timeout_at` 到点时写入。
- 已终结的 Run 不再接受状态变更，返回 `RUN_TERMINAL` 与当前状态，Runner 停止重试。
- 重复上报返回首次结果并标记 `replayed=true`，不重复写成本、不重复建 PR。
- 状态写入成功后，由状态机服务在同一事务内重算子任务与任务状态，并落审计。

### 2.7 日志分片上报

`POST /api/runner/v1/runs/{run_id}/logs`，幂等键 `{run_id}:{shard_seq}`。

| 字段 | 说明 |
|---|---|
| shard_seq | 分片序号，从 1 开始递增 |
| stream | `stdout`、`stderr`、`event` 三选一 |
| size_bytes | 压缩前字节数，单分片 1 MB（可配） |
| sha256 | 压缩后内容哈希，用于完整性校验 |
| data | 压缩后的分片内容，支持 base64 或 multipart |

规则：

- 服务端按唯一约束去重，对象存储键为 `runs/{run_id}/logs/{shard_seq}.gz`，允许乱序到达，缺失分片单独记账。
- 重复上报同一分片返回 `received=true` 与下一个期望序号，不重复计费、不重复入库。
- 上传失败按 1、2、4、8、16 秒退避，5 次后每 60 秒重试；本地保留 7 天。
- Run 结束后仍缺分片，标记「日志不完整」，只影响日志展示，不改变 Run 结果与任务状态。
- 旧 Runner 只读状态下仍可补传其曾经持有租约的 Run 日志，返回 202；不接受其他 Run 的日志。

### 2.8 产出物上报

`POST /api/runner/v1/runs/{run_id}/artifacts`，幂等键 `{run_id}:{artifact_type}`。

代码类分两步：Runner 推送任务分支并上报分支名、基线分支、提交号；服务端用 GitHub App 创建 PR，正文按模板写入产品、版本、任务、验收标准、Run 摘要、上下文快照编号与成本链接。同一任务只保留一个 PR，重试 Run 更新正文并保留历史 Run 摘要，不重复创建。

非代码类也分两步：先申请产出物记录，拿到 15 分钟有效的预签名上传地址；上传完成后回调 `POST /artifacts/{artifact_id}/complete`，提交哈希与大小，服务端校验后置为可用。

规则：

- 只有在租约有效期内上报的分支与 PR 才被接受；租约失效后的上报返回 `RUN_READ_ONLY`。
- 分支命名沿用 `ai/{任务编号}-{简短描述}`，基线为版本发布分支，PR 目标为版本发布分支。
- M3 不上报 PR，只上报方案、草稿或变更集，由人评审后自行创建分支与 PR；M4 无 Run，产出物标记「人工产出」。
- 每个产出物必须关联 Run 与上下文快照；重复提交返回首次结果。

### 2.9 用量上报

`POST /api/runner/v1/runs/{run_id}/usage`，幂等键 `{run_id}:{usage_seq}`。

请求携带作用域（整个 Run 或续聊轮次）、轮次序号、模型名称、输入与输出 token、调用次数、执行工具与 Agent 版本、是否完整。

- 每一轮续聊单独上报，生成一条成本记录，归属原 Run 与原会话；续聊不新建 Run。
- 同一幂等键重复提交只计一次，避免成本翻倍。
- 适配器无法读取用量时，上报 `complete=false`，服务端标记「成本不完整」，不按 0 计。
- 用量与状态分开上报：状态先到、用量后到是正常情况，成本记录允许在 Run 结束后补齐。

### 2.10 幂等键总表

| 动作 | 幂等键 | 去重窗口 | 重复提交返回 |
|---|---|---|---|
| 设备注册 | 设备指纹 + 账号 | 永久 | 原 `runner_id`，令牌轮换 |
| 领取任务 | 子任务编号 + Run 序号 | 10 分钟缓存响应 | 首次租约令牌 |
| Run 状态 | Run 编号 + 状态序号 | 随 Run 留存 | 首次结果，`replayed=true` |
| 日志分片 | Run 编号 + 分片序号 | 随 Run 留存 | 首次接收结果 |
| 产出物 | Run 编号 + 产出物类型 | 永久 | 首次产出物与 PR |
| 用量 | Run 编号 + 用量序号 | 永久 | 首次成本记录 |
| 会话轮次 | Run 编号 + 轮次序号 | 随会话留存 | 首次轮次记录 |
| 缺陷提交 Issue | 缺陷编号 + Issue 模板版本 | 永久 | 首次 Issue 链接 |
| 创建 PR | Run 编号 + `pr` 类型 | 同任务永久 | 首次 PR，后续只更新正文 |

### 2.11 旧 Runner 恢复后的只读约束

租约过期、已被改派或已被回收后，原 Runner 恢复上线一律进入只读模式：

1. 允许补传日志分片，附在旧 Run 下，用于排查。
2. 不允许继续执行，必须终止本地执行工具进程。
3. 不允许推送分支、创建 PR、上报产出物；服务端返回 `RUN_READ_ONLY`，并停止续签仓库写令牌。
4. 不接受其状态变更；Run 的最终状态以服务端判定为准。
5. 已上报的 token 用量照常计入成本，未上报部分不计费，标记「成本不完整」。
6. 本地结果只作为排查证据保留，不进入验收与质量统计。

仓库写权限在凭据层再收一道：服务端按租约签发短时 GitHub App 安装令牌（最长 60 分钟），租约失效后不再续签，旧 Runner 手上的令牌最多 60 分钟后失效。这个时间窗是已知残余风险，列入第 7 章。

### 2.12 版本兼容窗口

- Runner 版本使用语义化版本号，服务端同时支持「当前主版本」与「上一个主版本」。
- 上一个主版本只允许补传在途 Run 的日志与用量，不允许领取新任务。
- 更老版本拒绝领取，返回 `RUNNER_VERSION_UNSUPPORTED` 并提示升级。
- 协议版本独立于产品版本：协议升级时服务端同时接受旧协议一个窗口，窗口结束后强制升级。
- 能力标志由适配器上报并落库；没有可执行入口或探测失败一律按不支持处理，不能仅凭版本号推断为支持。
- 服务端保留「最低可领取版本」与「最低只读版本」两个配置项，升级与回退由系统管理员操作并留痕。

### 2.13 断网与重连

| 阶段 | 判定 | Runner 行为 | 服务端行为 |
|---|---|---|---|
| 在线 | 心跳正常 | 正常领取与执行 | 正常续约 |
| 降级 | 连续 2 个周期无心跳 | 继续执行，日志写本地 spool | 标记离线，租约继续倒计时 |
| 不可达 | 中央服务不可达超过 10 分钟 | 暂停领取新任务，继续本地执行与缓存 | 无请求 |
| 重连且租约有效 | 恢复后心跳续约成功 | 继续原 Run，补传缓存日志 | 顺延租约 |
| 重连且租约过期 | 恢复后心跳返回 `granted=false` | 终止执行工具，转只读补传日志 | 已生成新 Run，旧 Run 终结 |

补充规则：

- 重连退避为 1、2、4、8、16、30、60 秒并加随机抖动，避免全员同时重连打满服务端。
- 本地 spool 保留 7 天；占用超过配置阈值（建议 2 GB）时暂停领取并告警，避免写满磁盘。
- 断网期间产生的状态与用量按顺序补报，服务端按幂等键去重；乱序到达时以序号大者为准，冲突写审计。
- 中央服务恢复后，调度中心先处理过期租约，再开放领取，避免旧 Runner 与新 Runner 同时认为自己持有任务。

### 2.14 接口清单

| 方法 | 路径 | 用途 | 幂等键 |
|---|---|---|---|
| POST | `/api/runner/v1/register` | 注册与令牌签发 | 设备指纹 + 账号 |
| POST | `/api/runner/v1/heartbeat` | 心跳、续约、拉指令 | 请求编号 |
| POST | `/api/runner/v1/leases/claim` | 领取任务 | 子任务 + Run 序号 |
| DELETE | `/api/runner/v1/leases/{lease_id}` | 主动释放租约 | 租约编号 |
| POST | `/api/runner/v1/runs/{run_id}/state` | 状态上报 | Run + 状态序号 |
| POST | `/api/runner/v1/runs/{run_id}/logs` | 日志分片 | Run + 分片序号 |
| POST | `/api/runner/v1/runs/{run_id}/artifacts` | 产出物上报 | Run + 类型 |
| POST | `/api/runner/v1/artifacts/{id}/complete` | 非代码产出物完成回调 | 产出物编号 |
| POST | `/api/runner/v1/runs/{run_id}/usage` | 用量上报 | Run + 用量序号 |
| POST | `/api/runner/v1/runs/{run_id}/turns` | 续聊轮次记录 | Run + 轮次序号 |
| POST | `/api/runner/v1/diagnostics` | 环境自检结果 | 设备 + 时间窗 |

### 2.15 错误码

| 错误码 | HTTP | 含义 | Runner 处置 |
|---|---|---|---|
| `RUNNER_AUTH_INVALID` | 401 | 设备令牌无效或已吊销 | 停止上报，提示重新绑定 |
| `RUNNER_VERSION_UNSUPPORTED` | 426 | 版本低于可领取下限 | 停止领取，提示升级 |
| `LEASE_NOT_OWNER` | 403 | 租约不属于该 Runner 或令牌不匹配 | 放弃本次操作，不重试 |
| `LEASE_EXPIRED` | 409 | 租约已过期或已回收 | 转只读，终止本地执行 |
| `RUN_TERMINAL` | 409 | Run 已终结 | 停止上报该 Run |
| `RUN_READ_ONLY` | 409 | 只读模式下提交了禁止的操作 | 仅保留日志补传 |
| `STATE_SEQ_CONFLICT` | 409 | 状态序号倒序或跳号 | 以服务端状态为准，重新同步 |
| `QUOTA_EXCEEDED` | 429 | 并发配额不足 | 退避后重新领取 |
| `CENTRAL_UNAVAILABLE` | 503 | 服务端暂不可用 | 按退避重连，继续本地执行 |

## 3. 数据模型

### 3.1 建模约定

- 数据库为 PostgreSQL 16，表名与字段名用下划线小写；每张表都有 `created_at`、`updated_at`（日志与审计表只有创建时间）。
- 主键默认用 `bigint generated always as identity`，对外展示的业务编号单独建唯一索引；租约表使用 `uuid`，便于离线生成且不可猜测。
- 业务状态存英文码，页面映射为规格书的中文状态名，避免中文值参与逻辑判断；状态列用 `varchar` 加 `check` 约束，便于后续增加取值。
- 金额用 `numeric(14,4)`，币种单独存 `char(3)`，本期固定为 CNY；时间统一 `timestamptz`，以服务端时间为准。
- 外键默认 `on delete restrict`；只有附件、产出物允许逻辑删除，其余对象不做物理删除。
- 快照、配置、评审明细这类只读整体用 `jsonb`；需要被查询、统计、做唯一约束的字段一律拆成独立列。
- 审计日志、成本记录、日志分片元数据按只追加处理，应用账号不授予 `update` 与 `delete` 权限。

### 3.2 状态码映射

| 层 | 数据库状态码 | 规格书状态名 |
|---|---|---|
| 任务 | `draft` / `plan_pending` / `queued` / `executing` / `waiting_human` / `review_pending` / `acceptance_pending` / `completed` / `failed` / `cancelled` | 草稿 / 待确认计划 / 排队中 / 执行中 / 待人工 / 待评审 / 待验收 / 已完成 / 已失败 / 已取消 |
| 子任务 | `not_started` / `queued` / `executing` / `waiting_human` / `produced` / `completed` / `failed` / `cancelled` | 待开始 / 排队中 / 执行中 / 待人工 / 已产出 / 已完成 / 已失败 / 已取消 |
| Run | `created` / `dispatching` / `running` / `produced` / `failed` / `timed_out` / `interrupted` / `cancelled` | 已创建 / 等待执行 / 执行中 / 已产出 / 失败 / 超时 / 已中断 / 已取消 |

任务的 `queued` 在依赖未完成时只在展示层加「等待依赖」后缀，不新增状态码。

### 3.3 规格对象与表

| 规格书对象 | 表名 | 说明 |
|---|---|---|
| 产品 / 版本 / 需求 | `product`、`product_version`、`requirement` | 基础归集 |
| 任务 / 子任务 | `task`、`subtask` | 工作项两层 |
| 执行 Run | `run` | 一次执行尝试 |
| 领取租约 | `run_lease` | 独立成表，便于过期扫描与审计 |
| 事务日志分片 | `run_log_shard` | 只存元数据，内容在对象存储 |
| 产出物 | `artifact` | 分支、PR、文档、报告、标注、附件 |
| 上下文快照 | `context_snapshot` | 不可变，含材料引用与哈希 |
| 成本记录 | `cost_record` | AI、人工、补价调整 |
| 成员与权限点 | `account`、`member`、`permission_point`、`role_template`、`role_template_permission` | 账号、成员、权限点与模板 |
| 产品成员关系 | `product_membership`、`membership_permission_override` | 模板加逐项覆盖 |
| 测试执行 / 用例 / 结果 | `test_execution`、`test_case`、`test_case_result` | 缺陷聚合的事实来源 |
| 缺陷 | `bug`、`bug_case_result`、`bug_issue_sync_todo` | 含同步冲突待办 |
| 评审记录 | `review_record`、`acceptance_record`、`gate` | G2 与 G3 分开留痕 |

### 3.4 基础表

**account（账号）**

| 字段 | 类型 | 约束与说明 |
|---|---|---|
| id | bigint | 主键 |
| github_user_id | bigint | 唯一，GitHub 账号 ID |
| github_login | varchar(64) | 唯一，GitHub 用户名 |
| display_name / email / avatar_url | varchar / varchar / text | 展示信息 |
| status | varchar(16) | `pending` 待开通、`active` 已开通、`disabled` 已停用 |
| is_system_admin | boolean | 是否系统管理员 |
| first_login_at / last_login_at | timestamptz | 登录时间 |
| activated_by / activated_at | bigint / timestamptz | 开通人与时间，外键指向 `account.id` |
| disabled_at / disabled_reason | timestamptz / varchar(255) | 停用信息 |

索引：`unique(github_user_id)`、`unique(github_login)`、`index(status)`。

**member（成员）**

| 字段 | 类型 | 约束与说明 |
|---|---|---|
| id | bigint | 主键 |
| member_no | varchar(32) | 唯一业务编号 |
| account_id | bigint | 外键 `account.id`，唯一 |
| display_name | varchar(64) | 展示名 |
| team_roles | jsonb | 团队角色列表，可多个 |
| employment_status | varchar(16) | `active` 在职、`left` 离职 |
| default_role_template_id | bigint | 外键 `role_template.id`，默认权限模板 |
| left_at | timestamptz | 离职时间 |

索引：`unique(account_id)`、`index(employment_status)`。

**runner（Runner 设备）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| runner_no | varchar(32) | 唯一设备编号 |
| device_fingerprint | varchar(128) | 唯一设备指纹 |
| name / os / arch | varchar | 设备名、操作系统、架构 |
| agent_version | varchar(32) | Runner 版本 |
| status | varchar(16) | `pending` / `active` / `disabled`；在线、离线由最近心跳时间派生，不落状态 |
| bound_member_id | bigint | 外键 `member.id`，绑定成员 |
| max_concurrency | integer | 本机并发上限 |
| workspace_root_hash | char(64) | 工作目录根路径哈希 |
| last_heartbeat_at | timestamptz | 最近心跳时间 |
| last_seen_ip | inet | 最近请求来源 |
| config_version | varchar(32) | 最近配置版本 |
| registered_at / disabled_at / disabled_reason | timestamptz / timestamptz / varchar(255) | 注册与停用信息 |

索引：`unique(device_fingerprint)`、`index(bound_member_id, status)`、`index(last_heartbeat_at)`。

**runner_adapter_capability（设备适配器能力）**

`id`、`runner_id`、`adapter`、`tool_version`、`capability_flags`、`probe_status`、`probed_at`；联合唯一 `unique(runner_id, adapter)`，`probe_status` 取 `ok` / `partial` / `failed`。能力探测结果只由 Runner 上报，服务端不按版本号推断。

**product（产品）**

| 字段 | 类型 | 约束与说明 |
|---|---|---|
| id | bigint | 主键 |
| product_no / name / code | varchar | 业务编号唯一、名称、代号 |
| goal / scope | text | 目标与范围 |
| status | varchar(16) | `planning` / `active` / `paused` / `archived` |
| owner_member_id | bigint | 外键 `member.id`，产品负责人 |
| github_repos | jsonb | 仓库列表与默认分支 |
| allow_external_model | boolean | 允许外发开关，默认关闭 |
| context_package | jsonb | 产品级常驻上下文引用 |
| tags | jsonb | 标签 |

索引：`unique(product_no)`、`index(status)`、`index(owner_member_id)`。

**product_version（版本）**

| 字段 | 类型 | 约束与说明 |
|---|---|---|
| id | bigint | 主键 |
| version_no | varchar(32) | 唯一业务编号 |
| product_id | bigint | 外键 `product.id` |
| name / scope / goal | varchar / text / text | 名称、范围、目标 |
| status | varchar(16) | `planning` / `developing` / `release_pending` / `released` / `archived` |
| owner_member_id | bigint | 外键 `member.id`，版本负责人 |
| repo_full_name / release_branch | varchar | 默认仓库与发布分支 |
| planned_start / planned_end / actual_end | date | 计划与实际日期 |
| context_package | jsonb | 版本级常驻上下文 |

索引：`unique(product_id, version_no)`、`index(product_id, status)`。

**requirement（需求）**

| 字段 | 类型 | 约束与说明 |
|---|---|---|
| id | bigint | 主键 |
| product_id / version_id | bigint | 外键，版本可为空 |
| type | varchar(16) | `markdown` / `feishu` / `natural` / `prototype` |
| title / source_url / doc_version | varchar / text / varchar | 标题、来源、文档版本号 |
| summary | text | 摘要 |
| parse_status | varchar(16) | 解析状态 |
| authorization_status | varchar(16) | 飞书等来源的授权状态 |
| last_synced_at | timestamptz | 最后同步时间 |

索引：`index(product_id, version_id)`、`index(parse_status)`。

### 3.5 权限模型

**permission_point（权限点）**

| 字段 | 类型 | 说明 |
|---|---|---|
| code | varchar(8) | 主键，P01–P22 |
| name / description | varchar / text | 名称与说明 |
| scope | varchar(8) | `product` 或 `global` |
| grantable | boolean | 是否允许逐项授予 |
| version | integer | 权限点定义版本号 |

**role_template（角色模板）与 role_template_permission**

| 表 | 字段 | 说明 |
|---|---|---|
| role_template | id、template_no、name、scope、is_builtin、version | 5 个产品模板加全局模板，`unique(template_no, version)` |
| role_template_permission | role_template_id、permission_code | 联合主键，外键分别指向模板与权限点 |

模板变更只影响新授权与重新套用，已授权成员保留原权限点，因此模板必须带版本号，不做原地覆盖。

**product_membership（产品成员关系）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| product_id / member_id | bigint | 外键，联合唯一 |
| role_template_id | bigint | 外键 `role_template.id` |
| product_role | varchar(16) | `owner` / `admin` / `version_owner` / `member` |
| joined_at / granted_by | timestamptz / bigint | 加入时间与授权人 |
| status | varchar(16) | `active` / `invalid`，离职或被移出后置无效 |
| change_log_ref | bigint | 外键 `audit_log.id`，最近一次变更 |

索引：`unique(product_id, member_id)`、`index(member_id, status)`。所有产品数据查询都必须先命中这条关系。

**membership_permission_override（逐项覆盖）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| membership_id | bigint | 外键 `product_membership.id` |
| permission_code | varchar(8) | 外键 `permission_point.code` |
| effect | varchar(8) | `grant` 追加、`revoke` 移除 |
| reason / operator_id | varchar(255) / bigint | 原因与操作人 |

索引：`unique(membership_id, permission_code)`。最终权限 = 模板权限 + 追加 − 移除，服务端每次请求实时计算，不缓存过夜。

### 3.6 工作项与执行

**task（任务）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| task_no | varchar(32) | 唯一业务编号 |
| product_id / version_id / requirement_id | bigint | 外键，需求可为空 |
| title | varchar(255) | 标题 |
| task_type / difficulty / risk_level | varchar(16) | 任务类型、L1–L4、低中高 |
| execution_mode | varchar(8) | `M1`–`M4` |
| status | varchar(24) | 见 3.2 |
| priority | varchar(4) | `P0`–`P2`，默认 P1 |
| owner_member_id / acceptance_member_id | bigint | 负责人、验收人 |
| acceptance_criteria | jsonb | 验收标准清单快照 |
| acceptance_criteria_version | varchar(16) | 当前冻结版本号 |
| context_package | jsonb | 上下文包配置 |
| latest_snapshot_id | bigint | 外键 `context_snapshot.id`，最近一次快照 |
| route_tool / route_agent_version / route_model_tier | varchar | 路由结果 |
| route_dual_run | boolean | 是否双跑 |
| route_rule_version | varchar(16) | 路由规则版本号 |
| subtask_total / subtask_done | integer | 子任务总数与已产出或已完成数 |
| estimated_cost / actual_cost | numeric(14,4) | 预估与实际成本 |
| failure_reason / cancelled_reason | text | 失败或取消原因 |
| queued_at / started_at / completed_at | timestamptz | 排队、开始、完成时间 |

索引：`unique(task_no)`、`index(version_id, status)`、`index(owner_member_id, status)`、`index(priority, queued_at)`。任务状态是派生值，只由状态机写入，不提供手工改状态的接口。

**task_dependency（任务依赖）**

`task_id`、`depends_on_task_id` 联合主键，外键均指向 `task.id`，`check (task_id <> depends_on_task_id)`；索引 `index(depends_on_task_id)` 用于反查后继。

**subtask（子任务）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| subtask_no | varchar(32) | 唯一业务编号 |
| task_id | bigint | 外键 `task.id` |
| title | varchar(255) | 标题 |
| executor_type | varchar(8) | `human` 或 `runner` |
| assignee_member_id / assignee_runner_id | bigint | 指定成员或 Runner，外键 |
| status | varchar(24) | 见 3.2 |
| current_run_id | bigint | 外键 `run.id`，当前活跃 Run |
| run_count / auto_retry_count | integer | 累计 Run 数与连续自动重试次数 |
| queued_at / started_at / finished_at | timestamptz | 时间 |

索引：`unique(subtask_no)`、`index(task_id, status)`、`index(status, queued_at)` 供调度扫描。

`subtask_dependency` 与任务依赖同构：`subtask_id`、`depends_on_subtask_id` 联合主键。

**run（执行 Run）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| run_no | varchar(40) | 唯一业务编号 |
| subtask_id | bigint | 外键 `subtask.id` |
| run_seq | integer | 同一子任务内递增序号 |
| initiator_member_id / executor_member_id | bigint | 发起人与执行人，外键 `member.id` |
| runner_id | bigint | 外键 `runner.id` |
| adapter / tool | varchar(32) | 适配器与执行工具 |
| agent_version / model_name / model_tier | varchar | Agent 版本、模型与档位 |
| snapshot_id | bigint | 外键 `context_snapshot.id` |
| route_rule_version | varchar(16) | 路由规则版本 |
| session_id | bigint | 外键 `run_session.id` |
| execution_mode | varchar(8) | M1–M4 |
| status | varchar(24) | 见 3.2 |
| first_output_at / ended_at / last_active_at | timestamptz | 首次产出、结束、最后活动时间 |
| turn_count | integer | 会话轮次数 |
| input_tokens / output_tokens / call_count | bigint / bigint / integer | 用量汇总 |
| accumulated_cost | numeric(14,4) | 累计成本 |
| cost_completeness | varchar(16) | `complete` / `incomplete` |
| exit_code | integer | 进程退出码 |
| error_category | varchar(16) | `retryable` / `non_retryable` |
| failure_reason / interrupt_reason | text | 失败与中断原因 |
| is_dual_run / dual_run_group | boolean / varchar(40) | 双跑标记与双跑组 |

索引：`unique(subtask_id, run_seq)`、`index(subtask_id, status)`、`index(status, ended_at)`。
约束：同一子任务同一时刻只允许一个非双跑活跃 Run，使用部分唯一索引：

```sql
create unique index uq_run_single_active
  on run (subtask_id)
  where status in ('created', 'dispatching', 'running')
    and is_dual_run = false;
```

双跑的两条 Run 必须同组、同快照、同验收标准版本，由应用层在同一事务内成对创建；同一子任务的双跑组与单跑互斥，不能一边双跑一边再生成单跑 Run。

**run_lease（领取租约）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | uuid | 主键 |
| run_id | bigint | 外键 `run.id` |
| runner_id | bigint | 外键 `runner.id` |
| token_hash | char(64) | 租约令牌的 SHA-256，不存明文 |
| issued_at / expires_at / renewed_at | timestamptz | 签发、到期、最近续约 |
| renew_count | integer | 续约次数 |
| released_at / release_reason | timestamptz / varchar(32) | 释放时间与原因：完成、失败、超时、过期、改派、取消 |

索引：`unique(run_id) where released_at is null`、`index(expires_at) where released_at is null`、`index(runner_id, released_at)`。
约束：一个 Run 同时最多一条未释放租约；回收器按到期索引批量扫描，避免全表扫描。

**run_log_shard（事务日志分片）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| run_id | bigint | 外键 `run.id` |
| shard_seq | integer | 分片序号，从 1 开始 |
| stream | varchar(8) | `stdout` / `stderr` / `event` |
| size_bytes | integer | 压缩前大小 |
| sha256 | char(64) | 压缩后哈希 |
| storage_key | text | 对象存储键 |
| is_final | boolean | 是否为最后一片 |
| received_at | timestamptz | 接收时间 |

索引：`unique(run_id, shard_seq)`、`index(run_id, received_at)`、`index(received_at)`。不按时间分区，避免分区键与唯一约束冲突；归档时按 `received_at` 范围批量删除元数据，内容在对象存储侧归档。

**context_snapshot（上下文快照）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| snapshot_no | varchar(40) | 唯一快照编号 |
| task_id / run_id | bigint | 外键，`run_id` 唯一 |
| items | jsonb | 材料引用列表：材料 ID、类型、版本号、摘要、哈希、优先级 |
| total_tokens | integer | 估算总量 |
| excluded_count | integer | 被排除的敏感文件数 |
| content_hash | char(64) | 整体哈希 |

只允许插入，不允许更新；材料后续变化不改变已有快照。任务切换上下文后重新执行必须生成新快照。

**artifact（产出物）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| artifact_no | varchar(40) | 唯一业务编号 |
| task_id | bigint | 外键 `task.id` |
| run_id | bigint | 外键 `run.id`，人工产出为空 |
| type | varchar(16) | `branch` / `pr` / `document` / `test_report` / `annotation` / `attachment` |
| name / storage_key / external_url | varchar / text / text | 名称、存储位置、外部链接 |
| branch_name / base_branch / head_sha | varchar | 分支类专用 |
| version | integer | 产出物版本号 |
| status | varchar(16) | `pending` / `available` / `rejected` |
| accepted | boolean | 是否已验收 |
| deleted | boolean | 逻辑删除标记 |
| created_by | bigint | 外键 `member.id` |

索引：`unique(run_id, type) where run_id is not null and deleted = false`、`unique(task_id) where type = 'pr' and deleted = false`、`index(task_id, type)`。同一 Run 同一类型只保留一条有效产出物，重复上报走更新版本号；同一任务只允许一个有效 PR 产出物。

**run_session / run_session_turn（会话与轮次）**

| 表 | 字段 | 说明 |
|---|---|---|
| run_session | id、run_id、session_key、resumable、degraded、turn_count、started_at、last_active_at | 一个 Run 一条会话，`unique(run_id)` |
| run_session_turn | id、session_id、turn_seq、initiator_member_id、content_summary、input_tokens、output_tokens、cost_amount、created_at | `unique(session_id, turn_seq)` |

续聊只追加轮次与「最后活动时间」，不改 Run 的结束时间，也不改变产出物归属。

### 3.7 成本表

**cost_record（成本记录）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| cost_no | varchar(40) | 唯一业务编号 |
| record_type | varchar(8) | `ai` 或 `labor` |
| source | varchar(16) | `auto` 自动上报、`manual` 补录、`repricing` 补价调整 |
| product_id / version_id / task_id / subtask_id | bigint | 归集外键 |
| run_id | bigint | 关联 Run，人工成本可为空 |
| turn_seq | integer | 续聊轮次序号 |
| initiator_member_id / executor_member_id / runner_id | bigint | 发起人、执行人、设备 |
| tool / agent_version / model_name / model_tier | varchar | 执行工具与模型 |
| input_tokens / output_tokens / call_count | bigint / bigint / integer | 用量 |
| price_table_version | varchar(32) | 单价表版本号 |
| input_price / output_price / fixed_fee | numeric(14,6) | 输入、输出单价与固定调用费 |
| amount | numeric(14,4) | 金额 |
| currency | char(3) | 默认 CNY |
| occurred_at | timestamptz | 发生时间 |
| pricing_status | varchar(16) | `priced` / `pending` / `incomplete` |
| is_complete | boolean | 用量是否完整 |
| is_manual / approved_by | boolean / bigint | 补录标记与审批人 |
| idempotency_key | varchar(80) | 唯一，防重复计费 |

索引：`unique(idempotency_key)`、`index(product_id, occurred_at)`、`index(version_id, occurred_at)`、`index(task_id)`、`index(run_id)`。
补价不改写原金额，生成 `source=repricing` 的新记录，并在查询时同时返回原记金额与重算金额。

**model_price（单价表，辅助）**

`id`、`provider`、`model_name`、`input_price`、`output_price`、`currency`、`effective_from`、`source_url`、`version`；索引 `unique(provider, model_name, version)`、`index(effective_from)`。历史成本始终引用当时版本，不随改价变化。

### 3.8 测试与缺陷

**test_execution（测试执行）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| execution_no | varchar(40) | 唯一执行编号 |
| product_id / version_id / task_id | bigint | 外键，任务可为空 |
| stage | varchar(16) | 冒烟、回归、专项、验收 |
| executor_member_id | bigint | 外键 `member.id` |
| env_snapshot | jsonb | 环境快照：系统版本、模型、套餐、配置、网络、脚本版本 |
| total / passed / failed / blocked / skipped | integer | 五项计数 |
| conclusion | varchar(16) | `pass` / `fail` / `partial` |
| evidence | jsonb | 证据引用列表 |
| started_at / ended_at | timestamptz | 起止时间 |

索引：`unique(execution_no)`、`index(version_id, stage)`、`index(task_id)`、`index(conclusion)`。

**test_case（测试用例）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| case_no | varchar(40) | 唯一用例编号 |
| product_id / version_id | bigint | 外键，版本可空表示跨版本 |
| applicable_task_type | varchar(32) | 适用任务类型 |
| title / precondition / expected | varchar / text / text | 标题、前置条件、预期结果 |
| steps | jsonb | 步骤列表 |
| priority / case_type / applicable_env | varchar(16) | 优先级、类型、适用环境 |
| status | varchar(16) | `active` / `deprecated` |
| version | integer | 用例版本号 |
| created_by | bigint | 外键 `member.id` |

索引：`unique(case_no)`、`index(product_id, status)`、`index(applicable_task_type)`。

**test_case_result（用例结果）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| execution_id / case_id | bigint | 外键，联合唯一 |
| case_version | integer | 执行时的用例版本 |
| result | varchar(16) | `passed` / `failed` / `blocked` / `skipped` |
| actual_result / error_signature | text / varchar(255) | 实际结果与错误摘要 |
| evidence | jsonb | 截图、日志、复现步骤 |
| bug_id | bigint | 外键 `bug.id`，可为空 |
| executor_member_id | bigint | 执行人 |
| retry_count | integer | 同一执行内重试次数 |
| executed_at | timestamptz | 执行时间 |

索引：`unique(execution_id, case_id)`、`index(bug_id)`、`index(case_id, executed_at)`。同一执行内重复执行只更新结果与重试次数，不新增行。

**bug（缺陷）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| bug_no | varchar(40) | 唯一缺陷编号 |
| title / steps / expected / actual | varchar / text | 标题与四要素 |
| severity / priority | varchar(8) | 严重程度与优先级 |
| status | varchar(16) | `new` / `confirmed` / `fixing` / `pending_verify` / `closed` / `wont_fix` |
| version_id / task_id / fix_task_id | bigint | 所属版本、发现任务、修复任务 |
| fingerprint | char(64) | 去重指纹：错误摘要 + 步骤特征 + 环境关键项 |
| discovery_stage | varchar(16) | 冒烟、回归、专项、验收 |
| execution_id | bigint | 外键 `test_execution.id` |
| environment / evidence | jsonb | 环境与证据 |
| github_issue_no / github_issue_url | integer / text | Issue 编号与链接 |
| discovered_by / confirmed_by | bigint | 发现人与确认人 |
| sync_status | varchar(16) | `synced` / `pending` / `conflict` |
| reopen_count | integer | 重开次数 |
| last_synced_at | timestamptz | 最近同步时间 |
| closed_at | timestamptz | 关闭时间 |

索引：`unique(bug_no)`、`index(version_id, status)`、`index(fix_task_id)`、`index(github_issue_no)`。
去重约束：同一版本下未关闭缺陷的指纹唯一，使用部分唯一索引：

```sql
create unique index uq_bug_open_fingerprint
  on bug (version_id, fingerprint)
  where status not in ('closed', 'wont_fix');
```

命中已有指纹时只追加复现信息与证据，不新建缺陷；跨版本是否合并由业务规则决定。

**bug_case_result（缺陷与用例结果关联）**

`bug_id`、`case_result_id` 联合主键，外键分别指向 `bug.id` 与 `test_case_result.id`；一个缺陷可关联多条失败结果。

**bug_issue_sync_todo（同步冲突待办）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| bug_id | bigint | 外键 `bug.id` |
| direction | varchar(24) | `platform_to_github` / `github_to_platform` |
| field_path / payload | varchar / jsonb | 冲突字段与两侧取值 |
| status | varchar(16) | `pending` / `conflict` / `resolved` |
| resolution | varchar(16) | `keep_platform` / `keep_github` / `merge` |
| resolved_by / resolved_at | bigint / timestamptz | 处理人与时间 |

索引：`index(status, direction)`、`index(bug_id)`。只有冲突项进入待办，正常同步不落这张表。

### 3.9 评审与验收

**gate（闸门）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| task_id | bigint | 外键 `task.id` |
| gate_type | varchar(8) | `G1` / `G2` / `G3` / `G4` |
| status | varchar(16) | `pending` / `passed` / `rejected` |
| handler_member_id | bigint | 当前处理人 |
| basis_version | varchar(16) | 判断依据的版本号，如验收标准版本 |
| handled_at / created_at | timestamptz | 处理与创建时间 |

索引：`index(task_id, gate_type)`、`index(status, created_at)`。

**review_record（评审记录）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| task_id / gate_id / artifact_id | bigint | 外键，产出物可空 |
| reviewer_member_id | bigint | 外键 `member.id` |
| conclusion | varchar(16) | `approve` / `reject` |
| comment | text | 评审意见 |
| github_review_id / pr_number | bigint / integer | GitHub 评审与 PR 回写 |
| reviewed_at / created_at | timestamptz | 评审时间与记录创建时间 |

索引：`index(task_id, reviewed_at)`、`index(reviewer_member_id)`。
服务端在写入前校验：评审人不是发起人、不是计划确认人、不是 Agent 所有者，否则拒绝并提示重新指定。评审结论为拒绝时必须填写意见，且不能回写 GitHub 的「批准」状态。

**acceptance_record（验收记录）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| task_id | bigint | 外键 `task.id` |
| criteria_version | varchar(16) | 验收标准版本 |
| round | integer | 第几轮验收 |
| item_results | jsonb | 逐条结果与证据 |
| rejection_category | varchar(16) | 功能缺失、理解偏差、质量问题、需求变更 |
| acceptor_member_id | bigint | 验收人 |
| accepted_at | timestamptz | 验收时间 |

索引：`unique(task_id, criteria_version, round)`。验收标准冻结后新增或修改条目必须升版本，旧轮次记录不覆盖。

### 3.10 审计与幂等辅助表

**audit_log（审计日志，只追加）**

`id`、`actor_member_id`、`action`、`object_type`、`object_id`、`before`、`after`、`reason`、`request_id`、`created_at`。索引 `index(object_type, object_id, created_at)`、`index(actor_member_id, created_at)`。按 `created_at` 月度分区，主键为 `(id, created_at)`；应用账号只有插入权限。

**request_idempotency（请求幂等缓存）**

`idempotency_key` 主键、`scope`、`response_ciphertext`、`status_code`、`created_at`、`expires_at`。只用于「领取响应丢失后重放」这类需要返回原始响应的场景，普通写操作靠各业务表的唯一约束去重。

**outbox_event（外部副作用发件箱）**

`id`、`event_type`、`aggregate_type`、`aggregate_id`、`payload`、`status`、`attempts`、`next_retry_at`、`created_at`、`processed_at`。用于 GitHub PR 创建、Issue 同步、飞书同步、邮件通知；数据库事务只写发件箱，由 worker 至少一次投递，失败重试并落死信。

### 3.11 事务边界与并发控制

| 场景 | 做法 |
|---|---|
| 领取任务 | 单事务内 `FOR UPDATE SKIP LOCKED` 选候选，按产品加咨询锁串行化配额判断，创建 Run 与租约后提交 |
| 租约续约 | `update run_lease set expires_at = now() + interval '90 seconds' where id = ? and released_at is null and expires_at > now()`，影响行数为 0 即视为失败 |
| 租约回收 | 每 10 秒批量取 `expires_at < now()` 的租约，`FOR UPDATE SKIP LOCKED`，每批 200 条，写终结状态与审计 |
| 状态迁移 | `update run set status = ? where id = ? and status = any(?)`，行数为 0 说明状态已变化，读取最新状态后决定是否重试 |
| 子任务与任务派生 | 与 Run 状态同一事务，按「全部完成 / 任一失败 / 存在待人工」的优先级重算，不允许旁路更新 |
| 成本写入 | 以 `idempotency_key` 唯一约束兜底，冲突即忽略；金额由服务端按单价表版本计算，Runner 只报用量 |
| PR 创建与 Issue 同步 | 不放进业务事务，写 `outbox_event` 后由连接器执行，失败重试，保证「状态已提交但 PR 没建」可恢复 |

循环引用说明：`run.session_id` 与 `run_session.run_id`、`run.snapshot_id` 与 `context_snapshot.run_id` 互为引用。建表时先建主表并允许引用列为空，再补外键约束；应用层在创建 Run 与快照的同一事务内回填，禁止长期为空。

### 3.12 留存与归档

| 数据 | 在线留存 | 归档 | 说明 |
|---|---|---|---|
| 任务、子任务、Run、租约、评审、验收 | 永久 | 不归档 | 业务事实，只增不删 |
| 日志分片内容 | 3 个月 | 月度冷备 | 元数据永久保留，归档后可按 Run 取回 |
| 产出物文件 | 3 个月 | 月度冷备 | 代码以 GitHub 为准，附件与报告归档 |
| 上下文快照 | 永久 | 不归档 | 体积可控，复盘必需 |
| 成本记录 | 永久 | 不归档 | 支持任意周期重算 |
| 审计日志 | 永久 | 月度冷备 | 只追加 |

归档流程在每月 1 日 02:00 执行：先校验对象哈希，再复制到冷备介质，校验通过后删除在线副本；每月抽样 1% 做恢复演练，恢复顺序为数据库、对象存储、索引。

## 4. 部署拓扑

### 4.1 拓扑与网络分区

- 公网区：负载均衡与反向代理，只暴露 443，转发到 Web/API 与 GitHub webhook 入口。
- 应用区：Web/API 多实例、调度 worker 单活、连接器 worker、日志管道 worker。
- 数据区：PostgreSQL 主从、Redis 主备、对象存储，均不暴露公网。
- 成员设备：Runner 通过 443 出站访问应用区，同时按需访问模型服务与 GitHub。
- GitHub 事件同步的部署形态已由业务确认：采用公网 webhook 自动同步，不走人工触发；webhook 缺失或需要补历史时，人工触发同步作为兜底手段保留，处理结果同样写审计。

### 4.2 服务端组件

| 组件 | 实例 | 职责 | 故障影响 |
|---|---|---|---|
| 反向代理 | 2 | TLS 终止、限流、静态资源 | 单实例故障由另一个接管 |
| Web/API | 2 起 | 页面、业务接口、权限校验、Runner 网关 | 无状态，滚动重启；重启期间 Runner 重连 |
| 调度 worker | 1 主 1 备 | 发放租约、回收过期、重试、超时、改派 | 主故障后备接管，接管前不发放新租约 |
| 连接器 worker | 1 起 | GitHub webhook 处理、PR 与 Issue 同步、飞书同步、邮件 | 事件积压，恢复后按发件箱补发 |
| 日志管道 worker | 1 起 | 分片校验、对象存储写入、缺失检测、归档 | 上传积压，Runner 本地缓存兜底 |
| PostgreSQL | 1 主 1 从 | 唯一事实来源 | 主故障切换从库，切换期间只读 |
| Redis | 1 主 1 备 | 领取队列加速、限流计数、短缓存 | 丢失后从 PostgreSQL 重建，不丢业务状态 |
| 对象存储 | 3 节点或云托管 | 日志、附件、非代码产出物 | 上传失败重试，不阻塞 Run 状态 |

### 4.3 Runner 部署

- 交付形态：Windows 与 macOS 安装包，安装后常驻托盘程序，单文件可执行，不要求成员预装 Node、Git 之外的运行时。
- 安装后自检：Git 与 GitHub 凭据可用性、三款执行 CLI 是否安装及版本、Trae IDE 版本与 Hook 是否可用、模型账号连通性、磁盘余量、工作目录可写，自检结果随注册上报。
- 工作目录：每个任务独立目录 `workspace/{task_no}/{run_no}`，任务结束后保留 7 天，供人工排查与续聊；禁止多个 Run 共用目录。
- 本地缓存：spool 目录存日志分片与待上报结果，嵌入式 SQLite 只做索引；缓存上限默认 2 GB，超限暂停领取并告警。
- 凭证：设备令牌与仓库短时令牌存操作系统钥匙串；模型账号凭证由成员本机配置，平台只保存脱敏引用。
- 更新：默认提示更新，不静默升级；是否允许强制更新列入第 7 章未决项。

### 4.4 网络出入

| 方向 | 来源 → 目标 | 端口/协议 | 用途 | 控制 |
|---|---|---|---|---|
| 入站 | 公网 → 应用区 | 443 / HTTPS | 页面与 API | TLS、WAF、限流 |
| 入站 | GitHub → 应用区 | 443 / HTTPS | webhook | 签名校验、重放窗口 5 分钟、事件去重 |
| 入站 | 内网运维 → 应用区 | 443 / SSH 跳板 | 运维 | 白名单、双因素 |
| 出站 | 应用区 → GitHub | 443 / HTTPS | PR、Issue、仓库读取 | 固定出口 IP，最小权限安装令牌 |
| 出站 | 应用区 → 模型服务 | 443 / HTTPS | 服务端侧摘要与评测 | 按域名白名单 |
| 出站 | 应用区 → 飞书、SMTP | 443 / 587 | 文档同步与邮件 | 按域名白名单 |
| 出站 | Runner → 应用区 | 443 / HTTPS | 注册、心跳、领取、上报 | 设备令牌、双向校验可选 |
| 出站 | Runner → GitHub、模型服务 | 443 / HTTPS | 推分支、调用模型 | 由成员网络策略决定 |
| 禁止 | 公网 → Runner | 任意 | 无 | Runner 不监听入站端口 |

### 4.5 存储与容量

- 元数据：PostgreSQL 主库建议 4 核 8 GB、SSD 200 GB 起步；日志分片元数据按月分区，单条小，增长可控。
- 队列与缓存：Redis 2 GB 起步，只存领取队列、限流计数与 5 分钟级缓存，不作为事实来源。
- 对象存储：假设每日约 400 个 Run、单 Run 平均 20 MB，日增约 8 GB，3 个月热存约 720 GB；附件与报告另计。该估算需在试运行两周后按实际采样校准。
- 本地磁盘：单任务工作目录建议预留 10 GB；并发 4 个任务时成员设备至少保留 50 GB 可用空间。

### 4.6 备份与恢复

| 项 | 策略 | 指标 |
|---|---|---|
| PostgreSQL | 每月全量 + 每 12 小时增量（12:30 与 00:30），保留 30 天 | RPO 12 小时，RTO 2 小时 |
| 对象存储 | 跨节点冗余，月度冷备 | RPO 24 小时 |
| Redis | 每日快照，允许丢失 | 丢失后从 PostgreSQL 重建 |
| 配置与密钥 | 加密备份，变更即备份 | 恢复需双人确认 |

每季度做一次恢复演练，覆盖「数据库恢复到新实例」「对象存储取回指定 Run 日志」「凭据轮换后 Runner 重新注册」三项。

### 4.7 环境划分

| 环境 | 用途 | 数据 | 外部连接 |
|---|---|---|---|
| 开发 | 本地或开发机，单人使用 | 构造数据 | 可用测试仓库与测试模型账号 |
| 预发 | 发布前验证，配置与生产一致 | 脱敏副本或构造数据 | 独立 GitHub App 与 webhook |
| 生产 | 真实使用 | 真实数据 | 正式 GitHub App、正式模型账号 |

预发与生产使用不同的 GitHub App、不同的 webhook 密钥、不同的对象存储桶；禁止预发直接写生产仓库。

### 4.8 可用性与故障域

- 中央服务目标可用性 99%（工作时间口径），计划维护提前 24 小时通知。
- 中央服务故障不影响已领取任务的本地执行；Runner 按 10 分钟规则暂停领取，恢复后补传。
- 数据库主故障时进入只读，调度停止发放租约，已领取任务由租约倒计时与重连规则兜底。
- 连接器故障只影响 PR、Issue、飞书同步，不影响执行与验收；恢复后按发件箱补发。
- 对象存储故障时日志留在 Runner 本地，恢复后补传，不阻塞状态流转。

### 4.9 安全部署基线

- 全链路 TLS；数据库、Redis、对象存储仅内网访问，按服务账号最小权限授权。
- GitHub webhook 校验签名并限制重放窗口；PR 与 Issue 写操作使用 GitHub App 安装令牌，令牌按租约短时签发。
- 设备令牌、模型凭证、webhook 密钥存密钥管理服务或系统钥匙串，不写入日志与仓库。
- 日志与产出物访问走签名 URL，默认 15 分钟有效；下载行为写审计。
- 管理操作（成员开通、权限变更、Runner 吊销、版本兼容窗口调整）全部双人确认并留痕。

## 5. CLI 适配器

### 5.1 适配器契约

适配器是 Runner 内的独立模块。Codex、Claude Code、OpenCode 三款执行工具各一份适配器实现；Trae 不实现执行适配器，只做 IDE Hook 事件接入。三款执行适配器对外暴露同一组方法：

| 方法 | 输入 | 输出 | 说明 |
|---|---|---|---|
| `probe` | 可执行文件路径 | 版本、能力标志、探测时间 | 安装时与 CLI 升级后执行，结果上报服务端 |
| `prepare` | 工作目录、Run 载荷、Agent 版本 | 写入的文件、启动参数、环境变量 | 只写声明式配置，不生成可执行脚本 |
| `start` | 启动描述 | 子进程句柄、会话标识 | 不经过 shell，参数以数组传入 |
| `events` | 子进程输出 | 统一事件流 | stdout、stderr、结构化事件三路合并 |
| `sendTurn` | 会话标识、回复内容 | 是否成功 | 原生恢复或摘要注入两种路径 |
| `usage` | 事件流或结果文件 | token 用量与完整性 | 读不到就返回不完整 |
| `artifacts` | 工作目录、事件流 | 分支、提交号、文件清单 | 只采集，不创建 PR |
| `cancel` | 进程句柄 | 是否已终止 | 终止整棵进程树 |

适配器不判断业务状态、不直接写数据库、不调用平台接口；所有通信由 Runner 统一完成。

### 5.2 统一事件模型

Runner 把三款执行工具的原生输出归一为下列事件，每个事件带 `event_seq`、`ts`、`run_id`、`session_key`：

| 事件 | 关键字段 | 用途 |
|---|---|---|
| `session_started` | 会话标识、是否可恢复 | 记录会话，判断续聊路径 |
| `message_delta` | 角色、文本片段 | 实时展示与日志 |
| `tool_call` / `tool_result` | 工具名、参数摘要、结果摘要 | 审计实际动作与权限边界 |
| `file_change` | 路径、变更类型 | 产出物核对与评审提示 |
| `question` | 问题编号、问题、候选项 | 触发 G4 待人工 |
| `approval_required` | 动作、风险等级 | 转人工确认，不自动放行 |
| `usage` | 模型、输入输出 token、调用次数 | 成本记录 |
| `artifact_hint` | 分支、提交号、文件清单 | 产出物上报 |
| `error` | 分类、是否可重试、原始摘要 | 失败归因 |
| `completed` | 退出码、结果摘要 | 状态上报 |

原生输出无法解析成上述事件时，保留原始行写入 `event` 流，并标记「事件解析不完整」，不阻断执行。

### 5.3 进程管理与隔离

- 使用 `spawn` 直接启动可执行文件，参数以数组传递；任务标题、需求正文等用户内容通过提示词文件或标准输入传入，禁止拼进命令行，避免注入。
- 环境变量按白名单注入，只包含模型凭证引用、代理配置、工作目录与语言环境；不继承成员的全部环境变量。
- 每个 Run 独立工作目录；启动前校验目录为空或属于该 Run，禁止在仓库根目录直接执行。
- 输出按字节流采集，达到 1 MB 即切片；下游消费慢时对子进程施加背压，避免内存无限增长。
- 取消与超时统一终止进程树：Windows 使用作业对象并在失败时退回 `taskkill /T /F`，macOS 使用进程组信号。
- 子进程退出后先落最终日志，再上报状态，保证「状态为失败时能看到失败现场」。

### 5.4 三款执行适配器与 Trae 接入

**Codex CLI**

- 启动方式：`codex exec` 非交互模式，开启 JSON 事件输出；工作目录为任务目录，模型与档位通过配置档与启动参数指定。
- Agent 注入：项目指令文件承载身份与系统提示词，配置档承载模型、审批策略与 MCP 工具；沙箱模式映射可读写目录与联网边界。
- 会话恢复：从事件中取会话标识，按原生恢复参数继续；P2 接入 app-server 双向 JSON-RPC，支持流式、审批与续聊。
- 用量：从 JSON 事件读取 token；字段缺失时标记不完整。
- 权限边界：目录、命令、联网、审批策略均可由沙箱与配置强制，是三款执行 CLI 中强制项最完整的。

**Claude Code CLI**

- 启动方式：`claude -p` 打印模式，输出格式为流式 JSON；工作目录为任务目录。
- Agent 注入：子 Agent 定义文件承载身份与技能，项目指令文件承载团队约定，权限配置文件承载工具允许与拒绝清单；启动参数再叠加本次 Run 的工具限制。
- 会话恢复：从结果事件读取会话标识，按原生恢复参数继续。
- 用量：结果事件带用量字段，但部分版本字段不全，按「部分」处理，缺失即标记不完整。
- 权限边界：工具范围、目录、命令可强制；联网与内部信息范围只能申报加审计。

**Trae（P2，无执行适配器）**

- 实测结论：本机没有 `traecli`、`trae-cli`、`trae` 任何可执行文件；安装的是 Trae CN 桌面版 3.4.15，唯一命令行入口是 `trae-cn`，子命令只有 `chat`、`serve-web`、`tunnel`。
- 启动方式：`trae-cn chat` 把提示词交给图形界面后立即退出，标准输出只有 100 余字节生命周期日志，没有回答文本，退出码为空（图形子系统程序），不能作为非交互执行入口。
- 会话恢复：`--session-id` 不在已知选项列表。会话确实在 IDE 与服务端产生，但 CLI 无法指定或读取，不能恢复。
- 结构化产出与工具限制：`--json`、`--output-format`、`--allowed-tools` 均不被识别，没有可解析的结构化输出，也没有工具白名单参数。
- 用量：CLI 无输出，界面本地日志检索不到 token 字段，判断只在服务端；平台无法按 Run 或轮次归集成本。
- 权限边界：界面侧有命令规则文件与沙箱模块（文件权限、进程白名单、exec），但由 IDE 内部运行时读取，没有对外的命令行契约，只能申报，不能强制。
- 接入方式：只保留 IDE 内人工触发、Hook 回传事件（会话开始、提交提示、工具调用前后、停止、通知）的路径，归为 P2；平台不能反向启动 IDE 内的 Agent，也不为它创建 Run 领取租约。
- 路由结论：不进入 L3/L4 无人值守路由，也不作为 M1/M2 执行工具；L3/L4 由 Codex、Claude Code、OpenCode 覆盖。

**OpenCode**

- 启动方式：CLI 非交互模式或本地 server 模式，事件走 JSON 输出或事件流；工作目录为任务目录。
- Agent 注入：Agent 配置文件承载身份、模型与工具白名单，目录权限在配置中声明。
- 会话恢复：支持原生恢复，续聊不新建 Run。
- 用量：支持读取 token，按完整处理；读取失败时按不完整降级。
- 权限边界：目录与工具范围可强制；联网边界按配置与网络策略共同限制。

### 5.5 能力矩阵与降级

下表与规格 FR-20 的能力矩阵一一对应，技术侧只补充「怎么实现」与「缺能力时跑哪条路径」。Trae 列按本机实测结果标注，不沿用「待验证」的假设。

| 能力项 | Codex CLI | Claude Code CLI | Trae（P2） | OpenCode | 缺失时的技术路径 |
|---|---|---|---|---|---|
| Agent 定义载体 | 项目指令 + 配置档 | 子 Agent + 项目指令 | 无 CLI 契约，仅 IDE 内人工配置 | Agent 配置 + 工具白名单 | 只写系统提示词与工具清单，标注「Agent 注入不完整」，不得用于 L3/L4 |
| 可用工具与技能 | 沙箱 + MCP | 工具允许/拒绝 + 子 Agent | 无 CLI 契约，仅 IDE 内配置 | 工具白名单 | 降为最小工具集，页面标注 |
| 权限边界可强制项 | 目录、命令、联网、审批 | 目录、命令、工具范围 | 仅申报（IDE 内部沙箱无 CLI 契约） | 目录、工具范围 | 无法强制时只用于 M3/M4，页面标注「权限边界仅申报」 |
| 会话恢复 | 支持 | 支持 | 不支持（CLI 无法指定或读取） | 支持 | 新轮次携带历史摘要，页面标注「已降级为新轮次」，不新建 Run |
| 用量上报 | 部分 | 部分 | 不支持（CLI 无输出，仅服务端可见） | 支持 | 成本标记「成本不完整」，不按 0 计 |
| 结构化产出 | JSON | JSON | 不支持（无 JSON 与输出格式参数） | JSON | 解析自然语言，失败转人工确认，页面标注「需人工确认产出」 |
| 平台注入方式 | 项目指令 + 启动参数 | 子 Agent 与配置 + 启动参数 | 不支持（无 headless 入口，仅 Hook 回传） | Agent 配置 + 启动参数 | 注入失败时不启动执行，报环境自检失败 |

Trae 当前没有可执行入口，整行按不支持处理，不适用任何降级路径；只保留 IDE 内人工触发与 Hook 事件回传，路由规则不得把它列为可选执行工具。

### 5.6 差异处理规则

1. **能力以探测为准，不以版本号推断**：注册时与 CLI 升级后执行 `probe`，把能力标志写入 `runner_adapter_capability`；未探测或探测失败一律按不支持处理。
2. **路由硬校验**：M1/M2 要求权限边界至少有目录与命令两项可强制；L3/L4 要求具备非交互执行入口且 Agent 注入完整；选择双跑时两款工具都必须满足同一任务的硬性能力。
3. **降级必须可见**：任何降级同时写 Run 记录、页面标注、通知任务负责人；不允许静默降级。
4. **降级不跨级**：M1/M2 任务不能因为某款工具缺少强制能力就自动降到 M3/M4 执行，只能改用满足条件的工具或由人处理。
5. **成本不猜**：读不到用量就标记不完整，估算值单独列示，不混入实际成本。
6. **产出不猜**：解析不出结构化产出就转人工确认，不能把自然语言里的推测当成文件清单。
7. **续聊不重建**：会话恢复失败只降级为新轮次，仍在原 Run 下记账，不新建 Run、不重置验收标准。
8. **无入口即不支持**：`probe` 找不到可执行文件，或找到入口但没有非交互子命令时，直接判定不支持，禁止被路由选中；不为它创建降级路径，也不保留「待验证」状态。Trae 当前即此情况，只有 IDE Hook 事件接入，不是执行工具。

### 5.7 Agent 声明式配置翻译

平台下发的 Agent 定义只包含声明式字段：

| 字段 | 说明 | 翻译去向 |
|---|---|---|
| 身份与系统提示词 | Agent 的角色、约束、输出要求 | 各工具的项目指令或系统提示词文件 |
| 可用工具 | 允许与拒绝的工具、MCP 服务 | 工具配置、允许/拒绝清单、启动参数 |
| 技能清单 | 可用的技能与规则 | 技能目录或规则文件 |
| 权限边界 | 可读写目录、可执行命令、联网、提 PR | 沙箱模式、命令白名单、网络开关、平台令牌 |
| 默认模型档位 | 模型与档位 | 启动参数或配置档 |
| 版本号 | Agent 定义版本 | 写入 Run 记录，历史 Run 不随新版本变化 |

「是否允许提 PR」不依赖工具自身开关：平台只在租约有效时签发仓库写令牌，Agent 配置里的该项用于告知与审计，最终由令牌层控制。

### 5.8 权限边界落地

- 可强制项：目录、命令、联网、工具范围由执行工具的沙箱或白名单在本地强制；平台在 G1 确认计划时做前置校验，Run 结束后按事件流核对实际动作。
- 不可强制项：模型回复中携带的信息范围、工具内部的隐式网络访问，采用「申报 + 审计」；Agent 定义申报边界，Run 记录实际动作，审计按月抽查。
- 平台侧兜底：分支保护、PR 由平台创建、仓库写令牌短时有效、工作目录按 Run 隔离。
- Trae 当前只能申报：命令规则与沙箱是 IDE 内部模块，没有 CLI 契约，平台既无法在执行前强制校验，也无法在执行后按事件核对；因此只适用于 M3/M4 的人工路径，不进入无人值守执行。
- 越界处理：发现实际动作超出申报边界时，Run 标记「越界」，任务转人工复核，审计留痕，不自动重跑。

### 5.9 能力实测与版本维护

- 每款适配器维护「能力标志 + 适用版本区间」的映射表，放在 Runner 发布物内，随 Runner 升级更新。
- 安装时执行一次探测与最小样例执行，样例只验证启动、事件输出、会话标识与用量字段，不执行真实任务；探测结果上报服务端。
- 没有非交互入口的工具只登记「环境不可用」，不进入样例执行，不占用探测与样例执行配额；Trae 已登记为不可用，能力状态为不支持，等待官方无头 CLI 后再重新探测。
- CLI 主版本升级后自动重新探测；探测结果变化时更新能力标志，并在配置页提示受影响的任务类型。
- 能力状态只保留「支持 / 部分支持 / 不支持 / 环境不可用」四种，不再保留「待实测」；未探测或探测失败一律按不支持处理，不得被路由规则选为可用工具。

## 6. 关键流程时序

### 6.1 一次任务从排队到 PR

参与方：任务负责人、调度中心、Runner、适配器、GitHub、评审人。

| 步骤 | 发起方 | 动作 | 状态变化 | 失败处理 |
|---|---|---|---|---|
| 1 | 任务负责人 | 拆分任务、确认计划（G1）、冻结验收标准版本 | 任务草稿 → 排队中 | 计划被驳回则留在待确认计划 |
| 2 | 调度中心 | 生成上下文快照（材料、版本、哈希、排除项），计算路由（工具 + Agent + 模型档位） | 快照落库，任务排队中 | 材料不可读则转待人工，不生成 Run |
| 3 | 调度中心 | 选取子任务，创建 Run 与租约 | 子任务排队中 → 执行中；Run 等待执行 | 无可用 Runner 则保持排队并提示原因 |
| 4 | Runner | 领取租约，校验令牌与载荷，准备独立工作目录，检出发布分支 | Run 执行中 | 检出失败按可重试失败上报 |
| 5 | 适配器 | 写入 Agent 声明式配置，启动 CLI 子进程，开始采集事件 | Run 执行中 | 启动失败标记环境错误，可重试 |
| 6 | Runner | 每 30 秒心跳续约，按 1 MB 分片上传日志，遇到提问转 G4 | 任务执行中 → 待人工 → 执行中 | 租约丢失立即停止执行并转只读 |
| 7 | Runner | 采集用量与变更，推送任务分支，使用短时令牌写入 | Run 执行中 | 推送失败按可重试失败处理，分支不残留 |
| 8 | Runner | 上报分支产出物，服务端写发件箱 | Run 已产出；子任务已产出 | 重复上报返回首次结果 |
| 9 | 连接器 | 通过 GitHub App 创建 PR（M1/M2），正文写入任务、验收标准、Run、快照与成本链接 | 任务待评审 | 创建失败按发件箱重试，页面提示未建 PR |
| 10 | 评审人 | 评审分支或 PR；GitHub 评审回写评审记录 | 任务待评审 → 待验收或排队中 | 驳回必须写意见，回到排队并生成新 Run |
| 11 | 验收人 | 按冻结版本逐条验收 | 任务待验收 → 已完成 | 任一驳回按驳回原因回到排队 |
| 12 | 连接器 | 接收 PR 合并与评审 webhook，更新时间线与产出物状态 | 产出物已合并 | webhook 丢失按定时对账补偿 |

补充：

- 代码类任务必须评审分支或 PR；非代码类以文档或报告作为产出物，同样走 G2。
- 同一任务只保留一个 PR；重试 Run 更新正文中的 Run 摘要，不新建 PR。
- 双跑时两条 Run 同快照、同验收标准、各自分支与 PR，只指定一个 PR 参与合并，另一条留作对比记录。
- 任务完成以「全部子任务完成且 G3 通过」为准，任一子任务失败或待人工时任务不进入完成态。

### 6.2 断网、租约过期与改派

这三件事的触发条件与处置不同，不能合并成一条：

| 事件 | 触发条件 | 处置 | 是否换人 |
|---|---|---|---|
| 心跳离线 | 连续 2 个心跳周期（60 秒）无请求 | 标记离线，Run 不立即失败，租约继续倒计时 | 否 |
| 租约过期 | 90 秒未续约 | Run 置「已中断（租约过期）」，子任务回排队，按规格 7.4 生成新 Run | 否，仍由原执行成员领取 |
| 离线满 30 分钟 | Runner 仍不可用 | 告警，按规格 4.5 校验后改派到其他在线 Runner | 是 |

**租约过期与重连**

1. 回收器每 10 秒扫描到期租约，把 Run 置「已中断（租约过期）」，子任务回到「排队中」，生成新 Run，序号递增；原 Run 不复活。
2. Runner 在到期前恢复心跳并续约成功，继续原 Run，补传缓存日志与结果。
3. Runner 在到期后恢复，心跳返回 `granted=false`；Runner 终止本地执行工具，转只读，只补传旧 Run 的日志与已产生用量。
4. 旧 Runner 不得推送分支、创建 PR 或上报产出物；仓库写令牌不再续签，最长 60 分钟后失效。

**离线满 30 分钟的改派**

1. 调度中心先确认目标成员满足规格 4.5 的七项校验：成员关系有效、具备执行任务与提交产出权限、设备在线且适配器匹配、仓库有写权限、允许外发、目标成员未持有同一任务的其他 Run、任务处于排队中或待人工。
2. 自动改派只在七项全部通过时执行；任一项不通过则保留任务，通知产品管理员与系统管理员，不静默失败。
3. 人工强制改派仅产品管理员可操作，必须填写放行原因，任务详情页标注「越权放行」并写审计。
4. 改派等于旧 Run 置「已中断（改派）」加新 Run 在新 Runner 执行；沿用原上下文快照与验收标准，不重新生成；历史 Run 的发起人不变，新 Run 的发起人变为目标成员。
5. 新 Run 从版本发布分支重新拉取工作目录，不复用旧 Runner 的工作目录，也不迁移旧 Run。

**分支冲突处理**

改派前读取远端同名任务分支：

- 分支不存在：直接按正常流程执行。
- 分支存在且尚未创建 PR：把旧提交号记入旧 Run 的排查证据，新 Run 执行前将分支重置到发布分支基线，避免两次执行的改动混在一起。
- 分支已存在且已创建 PR：不自动改派，转人工处理，避免覆盖已评审代码。

改派、中断、恢复各写一条审计，任务时间线展示「原执行人 → 新执行人」与中断原因。

### 6.3 重试与超时

**自动重试**

- 仅对可重试错误自动重试：网络中断、模型限流、执行工具异常退出且未产生有效产出、Runner 临时不可用、租约过期。
- 不可重试错误不自动重试：适配器未安装、仓库无权限、上下文必填缺失且未豁免、权限或凭证被拒、验收标准缺失、产物冲突且无法自动解决。
- 同一子任务连续自动重试最多 2 次；每次生成新 Run，序号递增，保留原 Run 的日志、用量与失败原因，不覆盖。
- 达到上限或遇到不可重试错误：子任务置「已失败」，任务按聚合规则置「已失败」，通知任务负责人；不进入待人工，也不无限重试。
- 上下文或验收标准变化时重新生成快照，不沿用旧快照；未变化时沿用同一快照，保证可比。

**人工重试**

- 由任务负责人或产品管理员在「已失败」状态下发起，必须填写原因，不设次数上限。
- 人工重试开启新的执行周期，生成新 Run；可选沿用原快照或重新生成，重新生成必须说明变更内容。
- 人工重试不计入自动重试次数，但每次留痕，避免靠反复点击掩盖真实问题。

**超时**

- 服务端在 `execution_timeout_at` 到点写入「超时」，不依赖 Runner 上报；默认领取后 120 分钟，按任务类型可配。
- Run 置「超时」，子任务置「待人工」，任务置「待人工」；不自动回排队、不自动改派、不自动判失败。
- 任务负责人只能从三个动作中选一个：继续（生成新 Run，继承上下文快照）、终止（任务置已取消）、换低成本路由（生成新 Run，使用新路由）。
- 超时前 5 分钟通过站内通知提醒执行人，提醒不改变状态；G4 人工问答的 30 分钟超时只升级通知，不改变任务与 Run 状态。

### 6.4 缺陷与 GitHub Issue 同步

| 步骤 | 发起方 | 动作 | 结果 |
|---|---|---|---|
| 1 | 测试执行 | 用例结果落库，失败项带错误摘要与证据 | 失败结果可按指纹聚合 |
| 2 | 平台 | 按错误摘要、步骤特征、环境关键项生成指纹，聚合成缺陷草稿 | 同指纹未关闭缺陷只追加复现信息 |
| 3 | 测试或产品 | 人工去重、确认严重度与优先级 | 缺陷状态「新建」→「已确认」 |
| 4 | 连接器 | 按统一模板创建 GitHub Issue，写回 Issue 编号与链接 | 缺陷同步状态「已同步」 |
| 5 | 任务负责人 | 一键转「缺陷修复」任务，带入步骤、证据、Issue 链接与原版本 | 生成修复任务并进入排队 |
| 6 | Runner | 修复分支与 PR 引用 Issue 编号 | PR 与 Issue 双向可见 |
| 7 | 连接器 | PR 合并 webhook 到达 | 缺陷进入「待验证」 |
| 8 | 测试 | 复测通过则关闭；复测失败则重开并累加重开次数 | 关闭时同步关闭 Issue |

同步规则：

| 字段 | 权威方 | 冲突处理 |
|---|---|---|
| 标题、复现步骤、期望、实际、严重度、优先级 | 平台 | GitHub 侧修改只提示，不自动覆盖平台 |
| 关联版本、关联任务、测试执行、用例结果 | 平台 | 不允许在 GitHub 侧修改 |
| Issue 编号、链接、评论 | GitHub | 平台只读回写 |
| 开放/关闭状态 | 平台状态机 | GitHub 提前关闭时平台保持「待验证」，生成冲突待办 |
| 指派人与里程碑 | GitHub | 平台只展示，不作为业务字段 |

- 同步使用 `bug_id + Issue 模板版本` 作为幂等键，重复 webhook 不重复建 Issue、不重复改状态。
- 冲突项写入 `bug_issue_sync_todo`，由测试或产品选择保留平台、保留 GitHub 或合并，处理结果写审计。
- 无测试执行的人工登记缺陷标记「人工登记」，不参与自动聚合，但仍可提交 Issue 与转修复任务。
- Issue 关闭不等于缺陷关闭；只有复测通过才关闭缺陷，防止「Issue 关了但没验证」。

### 6.5 状态迁移责任

| 对象 | 当前状态 | 触发 | 目标状态 | 前置条件 |
|---|---|---|---|---|
| 任务 | 草稿 | 提交 | 待确认计划 | 验收标准已填写 |
| 任务 | 待确认计划 | G1 通过 | 排队中 | 验收标准冻结版本 |
| 任务 | 排队中 | 子任务领取成功 | 执行中 | 至少一个子任务进入执行中 |
| 任务 | 执行中 | 子任务进入 G4 | 待人工 | 提问已记录 |
| 任务 | 待人工 | 回复或继续 | 执行中或排队中 | 原 Run 有效或已生成新 Run |
| 任务 | 执行中 | 子任务全部产出 | 待评审 | 产出物已提交 |
| 任务 | 待评审 | G2 通过 | 待验收 | 评审人满足隔离要求 |
| 任务 | 待评审 | G2 驳回 | 排队中 | 填写评审意见 |
| 任务 | 待验收 | G3 全部通过 | 已完成 | 按冻结版本逐条通过 |
| 任务 | 待验收 | G3 驳回 | 排队中 | 选择驳回原因分类 |
| 任务 | 已完成 | 撤销验收 | 待验收 | 产品管理员批准并记录原因 |
| 任务 | 执行中 | 重试耗尽或不可重试 | 已失败 | 自动重试不超过 2 次 |
| 任务 | 已失败 | 人工重试 | 排队中 | 填写原因，开启新执行周期 |
| 任务 | 非终态 | 取消 | 已取消 | 有权限人二次确认 |
| 子任务 | 待开始 | 依赖完成并分配执行者 | 排队中；M3/M4 直接为执行中 | 有明确执行者 |
| 子任务 | 排队中 | 领取并创建租约 | 执行中 | 并发配额有余量 |
| 子任务 | 执行中 | Run 已产出 | 已产出 | 产出物已提交 |
| 子任务 | 执行中 | Run 失败或中断 | 排队中 | 可重试且未达上限，生成新 Run |
| 子任务 | 执行中 | Run 失败或中断 | 已失败 | 达到上限或属于不可重试错误 |
| 子任务 | 执行中 | Run 超时或进入 G4 | 待人工 | 等待负责人选择或回复 |
| 子任务 | 待人工 | 回复 | 执行中 | 原 Run 有效，否则先生成新 Run |
| 子任务 | 待人工 | 继续、换路由或终止 | 排队中或已取消 | 按所选动作生成新 Run 或取消 |
| 子任务 | 已产出 | G2 通过且产出被接受 | 已完成 | 代码 PR 已合并或非代码产出已确认 |
| 子任务 | 已产出 | G2 驳回 | 排队中 | 填写意见，生成新 Run |
| 子任务 | 已失败 | 人工重试 | 排队中 | 有权限人操作并填写原因 |
| Run | 已创建 | 下发 Runner | 等待执行 | 适配器可用 |
| Run | 等待执行 | Runner 拉起执行工具 | 执行中 | 租约创建成功且工作目录就绪 |
| Run | 执行中 | 产出提交完成 | 已产出 | 租约有效 |
| Run | 执行中 | 可重试或不可重试错误 | 失败 | 记录错误分类 |
| Run | 执行中或等待执行 | 租约到期未续约 | 已中断 | 原因记租约过期或 Runner 离线 |
| Run | 执行中 | 超过执行超时阈值 | 超时 | 调度中心结束 |
| Run | 执行中 | 人为终止 | 已取消 | 有权限人操作 |

状态迁移只允许上表列出的路径，其他路径由服务端拒绝并返回当前状态；所有迁移写审计与任务时间线。终态 Run 不复活，重试、改派、换路由一律生成新 Run。

## 7. 风险与未决项

### 7.1 技术未决项

| 事项 | 当前默认 | 影响 | 决策人 | 决策时点 |
|---|---|---|---|---|
| 服务端技术栈最终确认 | 采用第 0.2 节的 Node.js + TypeScript 基线 | 招聘、交付节奏、运维习惯 | 技术负责人 | 编码开始前 |
| 日志检索方案 | 元数据存 PostgreSQL，正文存对象存储，先做按 Run 与关键字前缀检索；全文检索不做 | 排障效率、存储成本 | 技术负责人 | 试运行两周后按实际查询评估 |
| Runner 自动更新策略 | 只提示、不静默升级 | 兼容窗口收窄、安全补丁覆盖速度 | 产品负责人与技术负责人 | 内测开始前 |
| 仓库写权限模式 | GitHub App 短时安装令牌，按租约续签 | 旧 Runner 的 60 分钟残余写窗口 | 技术负责人与安全负责人 | 与 GitHub 联调前 |
| Trae 适配器启动方式 | 已实测无 headless CLI：暂停执行适配器开发，只保留 IDE Hook 回传（P2） | L3/L4 改由 Codex、Claude Code、OpenCode 覆盖，少一款可选工具 | 技术负责人 | 实测已完成且结论为负面；官方发布无头 CLI 后再评估启动方式 |
| Windows 进程树终止 | 作业对象优先，失败退回 `taskkill /T /F` | 取消后残留子进程，占用设备 | Runner 负责人 | Runner 首个版本 |
| 双跑 PR 归属 | 两条 Run 各自建 PR，只指定一个参与合并 | 验收口径、评审工作量 | 产品负责人 | 双跑上线前 |
| 缺陷指纹算法 | 错误摘要 + 步骤特征 + 环境关键项做规则哈希 | 误合并或漏合并 | 测试负责人 | 试运行前用历史样本校准 |
| 归档介质与恢复时长 | 月度冷备到离线磁盘，恢复按 Run 粒度 | 审计取数、故障恢复 | 运维负责人 | 首次归档前 |
| 容量与压测基线 | 按 100 并行 Run 估算 | 资源采购、租约回收延迟 | 运维负责人 | 上线前完成压测 |
| 会话摘要长度 | 最近 3 轮加工具结果摘要 | 续聊质量、上下文成本 | 技术负责人 | 适配器联调时 |
| 日志敏感信息处理 | 上传前按规则扫描并脱敏，命中即阻断上传 | 凭证与个人信息泄露 | 安全负责人 | 上线前 |

### 7.2 待业务确认

以下问题技术侧可以给出实现，但取值必须由业务或安全确认，不能由研发自行决定：

1. **仓库写操作的归属**：分支推送以执行成员身份还是以平台应用身份呈现。前者审计清晰，后者权限更容易收敛，影响提交作者与责任归属。
2. **旧 Runner 写窗口的可接受时长**：当前方案在租约失效后最长仍有 60 分钟写权限，业务是否接受；若不接受，需要改成服务端代推分支。
3. **双跑结果的采用规则**：两条 Run 的 PR 如何选择、另一条是否保留、是否计入质量统计与成本考核。
4. **缺陷跨版本合并**：同一指纹在多个版本出现时，是合并为一个缺陷还是按版本拆分；当前技术默认按版本隔离。
5. **人工成本口径**：M3/M4 的人工投入是否需要记录工时与折算成本，折算规则由业务给出。
6. **日志与对话内容的留存边界**：日志保留 3 个月已定，但对话正文、模型输入输出是否等同处理，是否允许用于评测与训练，需要书面确认。
7. **外发开关的粒度**：当前按产品级开关加成员设备白名单，是否需要细化到版本、任务类型或单个任务。
8. **强制改派的责任归属**：产品管理员越权放行后出现代码或数据问题，责任如何界定，需要在制度上明确。

### 7.3 风险登记

| 风险 | 影响 | 可能性 | 应对 |
|---|---|---|---|
| 旧 Runner 在租约失效后仍持有写令牌 | 已改派任务被旧设备覆盖分支 | 中 | 令牌最长 60 分钟、改派前检查分支、创建 PR 后禁止自动改派、审计告警 |
| 租约回收延迟导致两台设备同时执行 | 同一子任务产生两份代码 | 低 | 数据库唯一约束、回收器每 10 秒扫描、领取前二次校验租约状态 |
| Trae 桌面版 CLI 不具备执行能力 | 无法作为 L3/L4 执行工具；误配会导致任务无产出或卡死 | 已发生 | 暂停 Trae 适配器开发，L3/L4 由 Codex、Claude Code、OpenCode 覆盖，Trae 只保留 P2 |
| Trae 无 headless CLI（唯一入口 `trae-cn`） | 无法注册执行适配器，无法探测会话、用量与结构化产出 | 已确认 | `probe` 判不可用并禁止路由，页面标注「环境不可用」，等待官方 CLI |
| 日志体积超出估算 | 存储成本上升、上传阻塞执行 | 中 | 1 MB 分片、压缩、3 个月热存、按月采样校准 |
| GitHub webhook 丢失或重复 | PR 与 Issue 状态不同步 | 中 | 签名校验、事件去重、定时对账、发件箱重试 |
| 模型限流与价格波动 | 执行失败、成本失真 | 中 | 可重试分类、单价表版本化、成本不完整标记 |
| Windows 子进程残留 | 设备资源被占用、后续任务受干扰 | 中 | 作业对象、进程树终止、自检与告警 |
| 规格书继续修订导致口径漂移 | 实现与业务不一致 | 中 | 本文标注版本，规格书每次修订后同步核对相关章节 |
| 设备丢失或凭证泄露 | 仓库与模型账号被滥用 | 低 | 设备令牌可吊销、凭证存钥匙串、异常上报告警 |
| 归档恢复超时 | 审计取数不及时 | 低 | 季度恢复演练、按 Run 粒度索引、保留在线元数据 |

### 7.4 口径同步约定

- 本文对应业务规格书 v0.6.1；规格书后续修订涉及第 3、4、7、8、10 章时，同步检查本文的协议参数、表结构与流程描述。
- 数据模型的表名与字段是实现建议，规格书的业务对象与字段口径不变；二者冲突时以规格书为准。
- 本文不替代接口文档与建表脚本；开工后从本文派生 OpenAPI 契约与数据库迁移，派生件另立 Issue 管理。





