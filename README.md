# Visible Team / 可见协作

Visible Team 是一个 Codex Skill、轻量状态助手和可插拔 Provider 层。当前任务作为 Leader，既可以协调 Codex 侧边栏中可直接控制的 Worker，也可以在用户明确指定后连接 Antigravity 或 DeepSeek Harness 的原生会话。

它关注的是能力匹配，而不是角色扮演：Leader 保留重要判断、架构和最终整合，把边界清楚且适合其他模型的执行工作交给 Worker。创建 Worker 前先让用户确认分工；同一目标的后续阶段可以复用原来的 Worker 和模型安排。

## 主要能力

- 用户确认后才创建可见 Worker；
- 每个 Worker 可以独立选择模型和推理强度；
- Leader 与 Worker 可以双向提问、汇报和纠偏；
- 明显延续的任务不要求用户反复调用 Skill；
- 只向受影响的 Worker 同步相关上下文增量；
- 对跨阶段或跨任务工作，可使用无第三方依赖的 SQLite 助手保存协作状态；
- 用独立的交付握手、宿主观察和失败分类避免把 Worker 完成误当作协作完成；
- 提供紧凑 `resume` 摘要和可注入的 HostAdapter 边界，不伪造 Codex 宿主能力；
- 外部应用默认关闭，只有用户确认具体 Provider、模型、推理强度和权限模式后才允许启动；
- 用同一份状态记录 Codex thread ID、外部 native session/task ID 和可选打开引用；
- 统一显示 Codex rollout 或外部 Provider 原生返回的 Token 用量，缺失字段不估算；
- Antigravity 与 DeepSeek Harness 通过独立、可离线测试的轻量适配器接入；
- 不内置固定角色、代码流程或特定领域模板。

## 项目结构

```text
skills/visible-team/SKILL.md     Codex 协作规则
skills/visible-team/agents/      Skill 界面与自动调用策略
dsh-plugin/                      独立可安装的 DSH Visible Team 插件
scripts/visible_team_state.py    可选的持久状态助手
scripts/visible_team_external.py 经授权后执行一次外部 Provider 动作
scripts/visible_team_usage.py    只读的宿主 Token 用量查看
scripts/providers/              外部应用的轻量 Provider 适配器
skills/visible-team/references/  状态助手使用说明
tests/                           行为测试
.codex-plugin/plugin.json        Codex 插件清单
```

## 使用

在 Codex 中可以直接说：

```text
使用可见协作完成这个任务。
```

也可以自然语言说明模型偏好：

```text
架构由当前窗口判断，具体实现交给 Luna medium；先把分工给我确认。
```

当任务明显延续既有协作时，不必再次点名 Skill。分工、Worker 和共享背景没有实质变化时，应继续复用。

外部应用不会因为安装了插件而自动使用。需要时可以明确说：

```text
使用可见协作，把这个 Worker 放到 Antigravity；先确认模型、推理强度和权限模式，不要自动替换。
```

未指定外部应用时仍只使用 Codex 的正常可见任务。

外部 Provider 可以先做纯离线发现，不会发送模型请求：

```bash
python3 scripts/visible_team_external.py --provider antigravity discover
python3 scripts/visible_team_external.py --provider deepseek-harness \
  --dsh-sdk-path <deepseek-harness/python/sdk/src> discover
```

真正创建外部 Worker 时，必须先在协作状态中完成精确分配和
`authorize-worker`，执行命令还必须显式带上
`--confirm-authorized-dispatch`。这两道门用来避免“安装了插件就自动消耗其他
应用额度”。完整命令和能力边界见
[外部 Provider 说明](skills/visible-team/references/external-providers.md)。

## 持久状态助手

短任务不需要额外状态。预计跨阶段、跨任务窗口或存在多个 Worker 时，可以使用 `scripts/visible_team_state.py` 保存协作标识、Worker 映射、状态版本和定向上下文更新。

助手只负责确定性状态，不替模型决定任务如何拆分，也不替代 Codex 原生的可见任务和消息工具。Worker 的生命周期完成、Leader 收到/接受交付、宿主观察和整体 collaboration 完成是分开的状态。完整命令见 [状态助手说明](skills/visible-team/references/state-helper.md)。

需要核对宿主原生 Token 用量时，可运行 `scripts/visible_team_usage.py --db <chosen-db> --collaboration-id <stable-id> [--codex-home <codex-home>]`；加 `--json` 输出稳定 JSON。Codex 用量来自 rollout/state，外部用量来自适配器写入的原生计数，缺失明细会明确显示 `unavailable`。

## DSH Visible Team 插件

`dsh-plugin/` 是独立的 DSH 插件，不修改 DSH 源码。它在 DSH 的
`conversation.view` 注册 Team tab，把协作空间、定向 Context Packet 和
原生 Token observation 保存在插件自己的 SQLite 中。新 Agent 挂接时会生成一次
只指向该 Agent 的 objective/sharedRules bootstrap 包，由用户或 Leader 明确点击
投递；后续直接指挥不会重复注入。全局规则变化也不会隐式广播，需用明确目标创建
上下文增量。

UI 与未来 Leader 使用同一个 Host action contract：
`POST /api/visible-team/workspaces`，请求体是 `WorkspaceAction`。当前切片已提供
UI 和 HTTP 的可编程入口，但尚未注册 DSH 模型工具，因此尚未实现“自主 Leader
协作”；任何后续模型工具都必须转发到这一个 action contract。安装、兼容性和
开源归属见 [dsh-plugin/README.md](dsh-plugin/README.md)、
[dsh-plugin/ARCHITECTURE.md](dsh-plugin/ARCHITECTURE.md) 和
[dsh-plugin/COMPATIBILITY.md](dsh-plugin/COMPATIBILITY.md)。

## 设计边界

Skill 负责语义判断和协作方式；状态助手负责持久化、授权门、版本检查和去重；Provider 只把统一机械动作翻译成宿主原生命令或 SDK 调用。状态助手通过显式迁移保留旧数据；协调器会先持久化宿主动作请求，不确定创建结果时要求先核对再重试。当前版本不提供常驻后台服务、自动重试或跨应用消息传输的 exactly-once 保证，也不会把“保存了原生会话 ID”夸大成“已验证桌面应用必然出现可点击窗口”。如果 Provider 的原生接口不能逐次接收模型、推理强度或权限参数，插件只记录用户批准的期望值，并将实际转发能力标为不可用；它不会伪称设置已经生效。

## 来源与许可

项目以 MIT License 发布。可见任务协作思想派生自 `KeonSuYun/codex-team-orchestrator`；具体说明见 [NOTICE.md](NOTICE.md)。
