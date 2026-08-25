# Visible Team / 可见协作

Visible Team 是一个 Codex Skill 与轻量状态助手，用于让当前任务作为 Leader，协调用户可以在侧边栏中打开并直接控制的 Worker。

它关注的是能力匹配，而不是角色扮演：Leader 保留重要判断、架构和最终整合，把边界清楚且适合其他模型的执行工作交给 Worker。创建 Worker 前先让用户确认分工；同一目标的后续阶段可以复用原来的 Worker 和模型安排。

## 主要能力

- 用户确认后才创建可见 Worker；
- 每个 Worker 可以独立选择模型和推理强度；
- Leader 与 Worker 可以双向提问、汇报和纠偏；
- 明显延续的任务不要求用户反复调用 Skill；
- 只向受影响的 Worker 同步相关上下文增量；
- 对跨阶段或跨任务工作，可使用无第三方依赖的 SQLite 助手保存协作状态；
- 不内置固定角色、代码流程或特定领域模板。

## 项目结构

```text
skills/visible-team/SKILL.md     Codex 协作规则
skills/visible-team/agents/      Skill 界面与自动调用策略
scripts/visible_team_state.py    可选的持久状态助手
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

## 持久状态助手

短任务不需要额外状态。预计跨阶段、跨任务窗口或存在多个 Worker 时，可以使用 `scripts/visible_team_state.py` 保存协作标识、Worker 映射、状态版本和定向上下文更新。

助手只负责确定性状态，不替模型决定任务如何拆分，也不替代 Codex 原生的可见任务和消息工具。完整命令见 [状态助手说明](skills/visible-team/references/state-helper.md)。

## 设计边界

Skill 负责语义判断和协作方式；状态助手负责持久化、版本检查和去重。当前版本不提供常驻后台服务，也不声称对 Codex 宿主的消息传输提供额外的 exactly-once 保证。

## 来源与许可

项目以 MIT License 发布。可见任务协作思想派生自 `KeonSuYun/codex-team-orchestrator`；具体说明见 [NOTICE.md](NOTICE.md)。
