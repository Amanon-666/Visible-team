# dsh-visible-team

`dsh-visible-team` 是一个可独立安装/卸载的 DSH 插件。它把“协作空间”定义为
一个持久化的工作目标边界，而不是 DSH 的目录 Workspace：同一目录可以有多个
协作空间，也可以保存非代码目标。

## 当前切片

第一条纵向切片已经把以下闭环接通：

- 在 `conversation.view` 的 `Team` tab 创建、编辑协作空间的目标和共享规则；
- 挂接当前 DSH Session，或手动挂接已有的 Codex、Claude、ACP 等 native
  session/task；挂接不会创建第二个 Session；
- 新 Agent 挂接时，Host 在同一 SQLite 写事务内生成一个只指向该 Agent 的
  bootstrap Context Packet。它包含 objective/sharedRules，默认待投递；用户或
  Leader 点击“发送待同步”后才通过该 Agent 的 driver 发送并确认版本；
- 对已有 Agent 添加明确目标的上下文增量；`targets=all` 和隐式广播均被拒绝；
- 用户可以直接指挥空间内任意 Agent，也可以把 `leaderAgentId` 改为同空间内任意
  Agent。Leader 没有特殊 Session 所有权或固定角色；
- 只接收 provider 原生的 Token 观察值。未知字段保持 `null/unavailable`；重复的
  cumulative snapshot 不会被再次求和。幂等范围是
  `(agentId, source, observationId)`，因此不同 Agent 可以各自上报同名的原生 turn/event
  ID，driver 不必改写 provider ID。

“创建新 Agent”保留在统一 action contract 中，但只有注册了对应 provider 的
`driver.create` 才会执行；没有 driver 时返回 `capability-unavailable`，不会伪造
一个已创建的 Agent。当前内置 DSH adapter 只负责挂接/发送已有 DSH Session。

## 安装与安全边界

构建并安装到一个隔离 Profile：

```bash
pnpm install --ignore-scripts
pnpm test
pnpm build
DSH_HOME=/tmp/dsh-visible-team-profile-home dsh plugin --profile isolated add /absolute/path/to/dsh-plugin
```

上面的 `DSH_HOME` 应替换为一次性的临时目录，`isolated` 是 Profile 名称；不会
修改 DSH 正式 Profile。插件
只在自己的 `${DSH_HOME}/visible-team/workspace.sqlite`（或显式
`VISIBLE_TEAM_STATE_PATH`）保存状态。卸载时只撤销自己的 routes、slot 和数据库
连接，不删除 DSH 原生 Workspace、Session 或用户文件。

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

本切片尚未注册 DSH 模型工具，因此“Leader 自主调用 action contract”尚未实现；
目前可编程调用入口是 Host HTTP action API，UI 的 `TeamClient.dispatch()` 也只
是这个 API 的薄客户端。下一步若注册模型工具，工具必须转发到同一个 POST/action
实现，不能再建立一套 Leader 状态或 Session 所有权。

## DSH 适配边界

Host 插件硬依赖公开的 `webServer`。DSH 发送能力通过 Cordis 的可选子注入
`ctx.inject(["apiProxy"], ...)` 获取公开 `apiProxy.sessions.prompt`；不是从未声明
的 `ctx.get()` 偶然捕获。没有该服务或没有 `sessions.prompt` 时，插件仍可 attach
existing，但 DSH 发送/创建明确返回 `capability-unavailable`。因此不会出现看似
“已挂接”却静默丢消息的状态。

Client entry 声明并使用公开的 `slots`、`sessions` 注入，注册
`conversation.view` 的 Team tab；它不查询 DOM class、`document` selector，也不
覆盖 DSH sidebar/workspace 主区域。`sessions.open` 只用于打开用户已挂接的 DSH
Session。

外部 provider 通过 `AgentDriver` seam 接入。一个 native session/task 在数据库层
全局唯一，只能有一个 bridge/driver owner。relay 可以作为后续兼容 driver，但每次
relay 会增加一次中继模型调用；本切片没有自动 relay，最终目标是无模型的 provider
session proxy。

## 开源设施的使用范围

插件借鉴 `dsh-workbench` 的 MIT Cordis bundle/client Slot 注册方式和
`dsh-plugin-subagents` 的 provider/bridge seam；没有把它们的文件编辑器、终端、
任务编排、固定角色、队列或状态数据库搬入核心。Visible Team 的核心只认识
Collaboration Workspace、Agent Binding、Context Packet 和 Usage Observation。
归属与许可见 [NOTICE.md](NOTICE.md)，分层和兼容证据见
[ARCHITECTURE.md](ARCHITECTURE.md) 与 [COMPATIBILITY.md](COMPATIBILITY.md)。
