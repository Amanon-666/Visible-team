---
name: visible-team
description: Complete the user's original request through a user-confirmed allocation of work to visible project-task Workers, and continue an existing visible-team collaboration when the request clearly belongs to it. Use when the user asks for directly controllable Workers, separate Worker model choices, shared context, or continued coordination across tasks. Do not use for hidden subagents or requests that only ask for an orchestration report.
---

# Visible Team

## Keep the Original Task Primary

This skill changes how work is carried out, not what the user receives. The current task remains the Leader; do not change its selected model, reasoning effort, normal judgment, or ordinary ability to plan, implement, use tools, and finish the work.

- Deliver the program, document, analysis, media, or other outcome the user requested. A plan, worker list, dispatch summary, or progress report is not a substitute.
- Establish and retain whatever high-value reasoning, decisions, or structure the work needs, then delegate execution that benefits from a different capability or cost profile. The Leader may continue working directly when useful.
- Keep orchestration updates brief and secondary. The final response should focus on the completed task and material results.
- Do not add phases, roles, questions, checks, or ceremony merely because this skill is active.

Do not prescribe either model's reasoning steps, require chain-of-thought, or replace task-relevant skills and tools.

## Confirm the Allocation

Before creating tasks or beginning delegated execution, briefly propose what the Leader should retain, what each Worker should do, the proposed model and reasoning setting, and any important capability boundary. Ask the user to confirm or adjust it. Keep this proportional: a simple task may need only one sentence and one Worker, not artificial modules.

Invoking this skill, asking for visible collaboration, or authorizing the original task does not by itself approve a particular allocation. The allocation is approved only when the user has specified or accepted the relevant Leader/Worker division and model choices, or has explicitly authorized the Leader to choose the allocation and start without further confirmation.

Use a choice UI when available; otherwise ask one concise question. After proposing an unapproved allocation, stop and wait for the user's reply: do not create, contact, or wait on Worker tasks, and do not begin the delegated execution meanwhile. The Leader may inspect only what is reasonably needed to formulate the proposal. Once the user approves, proceed without asking again and do not repeat questions the user already answered.

## Continue Existing Collaboration

Treat an approved allocation as continuing across later user turns and work stages while they serve the same unfinished objective. Infer continuation from the user's references, the current task history, existing artifacts, shared context, and contactable Worker tasks. Reuse the approved division, model choices, and suitable existing Workers instead of asking again or creating duplicates.

Do not require the user to name this skill again when the new request clearly continues that collaboration. Confirm only a material change to the allocation, such as a new Worker, a different model or capability boundary, or a reassignment of consequential judgment. Changes within an already approved responsibility can proceed without renewed ceremony.

When work is likely to span stages, context compaction, or separate Leader tasks, retain a concise recoverable collaboration state in an actually accessible place or task record. The Leader chooses its location and form; preserve only what is useful to recover the objective, approved allocation, contactable Workers, material decisions, current state, and next work. Update it when those facts materially change, and mark the collaboration complete when the objective is finished so unrelated later work does not inherit stale coordination.

In a separate Leader task, inspect available project context and visible task records when there is evidence that the request continues existing work. Continue without reapproval when the relationship and allocation are clear. If the evidence is insufficient and the distinction would change whether Workers are used, ask one concise continuity question. Do not continue the collaboration when the user asks for Leader-only work, declines Workers, or starts an unrelated objective.

## Use Durable State When It Adds Value

For ordinary short work, use the current task history and host task records; do not create coordination state merely because this skill is active. When work is likely to span stages, context compaction, separate Leader tasks, or multiple Workers, use the bundled deterministic state helper described in [references/state-helper.md](references/state-helper.md).

The helper complements rather than replaces judgment or host tools. Use it to retain stable collaboration and Worker identities, approved responsibilities, lifecycle state, versions, and targeted context deliveries. Plan a logical Worker before creating its visible task, then attach the returned thread ID. After an uncertain host result, reconcile actual visible tasks before retrying. Keep task decomposition, relevance, model choice, semantic review, and corrections with the Leader.

## Use Suitable Visible Workers

- After confirmation, create Workers only within the approved allocation. Prefer the smallest useful number.
- Match work to cognitive demand, not to the surface label of the request. One deliverable may contain both judgment-heavy decisions and well-defined execution.
- Compare that demand with the current Leader and available Worker capabilities; do not assume the Leader is always strongest or that a lower-cost Worker is always sufficient.
- A single, sequential task may still benefit from a Worker when a portion is better matched to another model; parallelism or multiple deliverables are not required.
- When the whole task is well-defined and comfortably within a suitable Worker's capability, prefer delegating its execution while the Leader supervises and returns the result, unless coordination overhead outweighs the benefit or the user asks for Leader-only work.
- Keep consequential interpretation, architecture, strategy, and integration with the model best suited to them. Delegate a portion when it is concrete enough for the chosen Worker to execute reliably. If execution exposes a material unresolved decision, return it to the Leader instead of guessing.
- Workers are normal Codex project tasks that the user can open and instruct directly. Do not substitute temporary or hidden subagents.
- Choose each Worker model separately without changing the Leader. Honor the user's model, capability, cost, and reasoning preferences.
- Choose for reliable task completion first. Among suitable choices, prefer lower cost when that matters to the user.
- Beyond allocation confirmation, ask only when a material preference cannot reasonably be inferred. Do not claim an unavailable or unconfirmed model or silently substitute one.

## Give Enough Shared Context

Write a readable, task-specific brief in the user's language. Give the selected Worker enough concrete context to act independently, without imposing a fixed template. The Leader decides what outcome, source material, constraints, scope, locations, completion standard, and handoff details matter.

Use relevant domain skills for domain technique. Do not permanently encode coding, document, paper, presentation, image, or research procedures here. Share decisions and evidence, not private chain-of-thought.

For multiple Workers or dependent stages, establish shared context when it will prevent divergence or repeated explanation. The Leader chooses an actually accessible location, format, contents, and update method, tells Workers how to use it, and keeps it useful as decisions change. Do not hardcode a path or schema.

Synchronize context by relevance, not by broadcast. Give each Worker only the background and updates its responsibility or dependencies require. When context changes, notify only affected Workers and identify the relevant change or source; do not make every Worker reread all shared context. A short delta message is preferable when it is sufficient. Ask a Worker to reread a larger source only when the change cannot be conveyed reliably in a smaller form, and bring other Workers in only when their inputs or outputs are affected.

## Create and Contact Tasks

Use the host's normal project-task tools when available:

- create Workers with `create_thread` in the project and execution environment appropriate to the work;
- pass an explicitly chosen Worker model and reasoning setting when supported;
- give each task a recognizable title and surface its created-task link or directive;
- treat creation as successful only when the host returns a user-visible task that can be identified and contacted;
- if a result is unclear, inspect existing tasks before retrying so a partial success does not become a duplicate.

If the required visible-task or independent-model capability is unavailable, explain the limitation. Do not pretend a hidden process or silent fallback is equivalent.

## Coordinate Through Completion

After dispatch, give a concise progress update without ending the task. When sustained coordination is requested, keep the Leader active: use `wait_threads` for bounded event waits, `read_thread` when detail is useful, and `send_message_to_thread` for answers, changed decisions, or corrections.

Tell Workers they may contact the source Leader when missing information, a conflict, or new evidence would materially affect the result. They may discuss a plan for such a reason, but should not manufacture objections, seek confirmation for routine judgment, or argue by default. When they can proceed reasonably, they should proceed.

Workers may report meaningful progress that changes a decision, reveals a blocker or risk, or enables the next stage. Routine activity needs no ceremonial reporting. The Leader checks progress when useful, not by constant polling.

When a Worker finishes or needs guidance, the Leader assesses the new information, updates shared context when useful, answers what it can, and adjusts or launches the next authorized work. Ask the user only when the decision depends on their preference, new authority, inaccessible information, or an unresolved conflict in their instructions.

Treat the user's latest instructions in any visible task as real input. Reconcile compatible changes; if a conflict would change the outcome, scope, or permissions, explain it and ask the user rather than letting tasks continue in different directions.

Do not end merely because Workers were created or one stage completed. Continue until the user's requested outcome is delivered, a necessary user decision is pending, or the host cannot continue. Do not promise unattended persistence after the Leader's turn ends unless the host provides and the user authorizes an appropriate continuation mechanism.

The Leader remains responsible for the final handoff. Worker output is not automatically verified; check and integrate it to the degree warranted by the actual task without imposing a fixed review ritual.
