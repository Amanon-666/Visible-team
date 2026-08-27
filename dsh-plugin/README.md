# dsh-visible-team

`dsh-visible-team` 是一个可独立安装/卸载的 DSH 插件。它把“协作空间”定义为
一个持久化的工作目标边界，而不是 DSH 的目录 Workspace：同一目录可以有多个
协作空间，也可以保存非代码目标。

## 当前切片

第一条纵向切片已经把以下闭环接通：

- 在 `conversation.view` 的 `Team` tab 创建、编辑协作空间的目标和共享规则；
- 在侧边栏底部提供始终可见的“协作工作台”入口；侧栏收起时保留图标，并通过
  tooltip 说明入口用途；
- 挂接当前 DSH Session，或手动挂接已有的 Codex、Claude、ACP 等 native
  session/task；挂接不会创建第二个 Session；
- 新 Agent 挂接时，Host 在同一 SQLite 写事务内生成一个只指向该 Agent 的
  bootstrap Context Packet。它包含 objective/sharedRules，默认待投递；用户或
  Leader 点击“发送待同步”后才通过该 Agent 的 driver 发送并确认版本；
- 对已有 Agent 添加明确目标的上下文增量；`targets=all` 和隐式广播均被拒绝；
- 用户可以直接指挥空间内任意 Agent，也可以把 `leaderAgentId` 改为同空间内任意
  Agent。Leader 没有特殊 Session 所有权或固定角色；
- 内置 DSH Driver 复用公开 `sessions.list/create/selectModel/prompt/history` 与
  `events.mux`，提供 discover、attach/create、send/resume、status/watch、usage
  的可验证能力矩阵；provider/model/thinking/permissions 缺失时拒绝 native 调用；
  `open` 在公开 Host ApiProxy 没有 session-specific UI 操作时保持 unavailable；
- 只接收 provider 原生的 Token 观察值。未知字段保持 `null/unavailable`；重复的
  cumulative snapshot 不会被再次求和。幂等范围是
  `(agentId, source, observationId)`，因此不同 Agent 可以各自上报同名的原生 turn/event
  ID，driver 不必改写 provider ID。
- 在 DSH 提供公开 `tools` 服务时注册一个 `visible_team` 模型工具。它只从
  `exec.agent.id` 解析当前已挂接的 DSH Agent，再由该绑定得到唯一协作空间；工具结果
  只返回短投影、上下文版本和截断标记，不把完整空间或上下文重复塞回模型。

“创建新 Agent”保留在统一 action contract 中，但只有注册了对应 provider 的
`driver.create` 才会执行；没有 driver 时返回 `capability-unavailable`，不会伪造
一个已创建的 Agent。内置 DSH adapter 只有在公开 `sessions.create` 与
`sessions.selectModel` 同时存在、且调用者提供完整 native route 时才创建真实
Session；调用者可通过 `Config.dsh.nativeProvider` 固定 DSH 的上游 provider，或在
action/driver 输入中显式提供它。

## 安装与安全边界

构建并安装到一个隔离 Profile：

```bash
pnpm install --config.auto-install-peers=false --ignore-scripts
pnpm test
pnpm build
DSH_HOME=/tmp/dsh-visible-team-profile-home dsh plugin --profile isolated add /absolute/path/to/dsh-plugin
```

Client CSS 直接由 `lightningcss` 编译；安装命令关闭 `tsdown` 可选 CSS peer，避免
引入未使用的 `@tsdown/css`。

上面的 `DSH_HOME` 应替换为一次性的临时目录，`isolated` 是 Profile 名称；不会
修改 DSH 正式 Profile。插件
只在自己的 `${DSH_HOME}/visible-team/workspace.sqlite`（或显式
`VISIBLE_TEAM_STATE_PATH`）保存状态。卸载时只撤销自己的 routes、slot 和数据库
连接，不删除 DSH 原生 Workspace、Session 或用户文件。

## 侧边栏入口与最小流程

插件加载后，DSH 侧边栏底部会一直显示“协作工作台”。侧栏收起时仍保留入口图标，
鼠标悬停可以看到“打开协作工作台”的提示。点击入口会打开较宽的工作台面板，
用于快速查看和新建协作空间；已有的 `Team` 标签页仍保留，用于当前会话的详细管理。

第一次打开且还没有空间时，按这个顺序即可开始：

1. 创建一个空间并填写名称与共同目标；
2. 打开空间后，在 `Team` 标签页关联会话；
3. 在同一个标签页选择 Leader，继续管理成员与上下文。

工作台只承担空间列表和创建空间的最小流程，不会替换 DSH 原生会话、Workspace
或侧栏。空间的详细编辑、会话关联和 Leader 操作仍共用原有 Host action contract。

## 一个 action contract，两个入口

UI 和未来的 Leader 都调用同一个 Host action contract：

```text
POST /api/visible-team/workspaces
body: WorkspaceAction
```

`WorkspaceAction` 在 `src/shared/types.ts` 定义，包含 `create-workspace`、
`update-workspace`、`attach-agent`、`create-agent`、`add-context`、
`deliver-context`、`send-agent`、`ack-context` 和 `record-usage`。GET 同一路径
读取空间快照；`GET /api/visible-team/context?workspace=...&agent=...` 是显式的
单 Agent 上下文读取。

模型入口是单一的 `visible_team` 工具；它不是另一套状态机，所有写操作都转发到同一个
Host `WorkspaceAction` 执行器：

- `list_workspaces` / `read_workspace` 只给当前绑定空间的 Leader 返回短快照；不会按
  请求里的 `workspaceId` 列举任意空间；
- `read_pending_context` 读取一个明确 Agent 的待收包。普通成员只能读取自己，Leader
  可以读取当前空间内已有 Agent；摘要按条数和字符数截断并返回版本号；
- `send_message` / `deliver_context` 只允许 `workspace.leaderAgentId` 与调用者严格相等
  的 Leader，目标必须是当前空间内已经挂接的 Agent；
- `progress` 对普通成员只写一条定向给 Leader 的短 Context Packet，Leader 可选择当前
  空间内已有目标；没有隐式广播。

调用者没有稳定 `exec.agent.id`、没有以 `provider=dsh` 挂接，或绑定查询失败时，工具直接
拒绝，不使用 `workspaceId` 猜测身份。模型工具不创建 Agent、不修改权限，也不把
`create-agent` 暴露在参数面上。Host 没有公开 `tools` 服务时，原有 UI、存储和 HTTP
入口仍可用，只是不注册模型工具。

## DSH 适配边界

Host 插件硬依赖公开的 `webServer`。DSH Driver 通过 Cordis 的可选子注入
`ctx.inject(["apiProxy"], ...)` 获取公开 `sessions.list/create/selectModel/prompt/history`
和 `events.mux`；不是从未声明的 `ctx.get()` 偶然捕获。缺少某个公开 face 时，
对应能力返回 `capability-unavailable`，不会用自建协议、PTY 或本地会话树补齐。
所有 native send/create 调用都要有明确 provider、model、thinking 和 permissions；
`Config.dsh.nativeProvider` 只用于显式固定上游 provider，不会触发额度探测。

模型工具同样通过公开的可选 `ctx.inject(["tools"], ...)` 注册，内部调用官方
`defineTool`、`ToolRuntime.register`、通用 `generic` tool card 和 Host 现有 action
执行器。官方 ToolRuntime 的 pre-execute/approval 管线仍由 DSH Host 负责；插件不复制
approval、路由、RPC、PTY 或 Agent 生命周期。身份授权是工具自身的 fail-closed 检查，
并且每次调用重新解析当前 `exec.agent`，不缓存 Leader 角色。

### 模型 Token 影响

工具 schema 固定只发送一次，包含一个 `operation` 枚举和少量可选字段；工具不会在每次
调用返回完整 workspace、`sharedRules`、targets 或 native 会话日志。`list/read` 返回
最多 24 个 Agent 的计数/状态短投影；pending context 最多 8 条、每条摘要最多 1,000
字符，并带 `pendingCount`/`truncated`；进度最多 2,000 字符，直接消息最多 8,000 字符。
因此读取上下文的成本与模型明确请求的少量摘要成正比，未请求的 Agent 不会产生上下文
Token。实际发送给 native Agent 的 Token 仍由对应 provider/driver 统计；插件不估算、
不重复计费。

Client entry 声明并使用公开的 `slots`、`sessions` 注入，注册
`conversation.view` 的 Team tab 和 root/list 作用域的 `sidebar.footer.action`。
后者只消费 DSH 公开的 `wide` owner 状态，在收起侧栏时保留可识别的图标入口；它不
覆盖 DSH sidebar/workspace 主区域。工作台复用 DSH 公开的 UI primitives、主题 token
和图标，并将自己的 CSS 作为带插件归属标记的模块样式注入。`sessions.open` 只用于
打开用户已挂接的 DSH Session。

外部 provider 通过 `AgentDriver` seam 接入。一个 native session/task 在数据库层
全局唯一，只能有一个 bridge/driver owner。relay 可以作为后续兼容 driver，但每次
relay 会增加一次中继模型调用；本切片没有自动 relay，DSH Driver 直接复用 native
Session，不实现通用 JSON-RPC、PTY 或第二套会话树。

## 开源设施的使用范围

插件借鉴 `dsh-workbench` 的 MIT Cordis bundle/client Slot 注册方式、`dsh-market`
的 MIT virtual-id/lightningcss Client CSS 构建方式和
`dsh-plugin-subagents` 的 provider/bridge seam；没有把它们的文件编辑器、终端、
任务编排、固定角色、队列或状态数据库搬入核心。Visible Team 的核心只认识
Collaboration Workspace、Agent Binding、Context Packet 和 Usage Observation。
归属与许可见 [NOTICE.md](NOTICE.md)，分层和兼容证据见
[ARCHITECTURE.md](ARCHITECTURE.md) 与 [COMPATIBILITY.md](COMPATIBILITY.md)。
