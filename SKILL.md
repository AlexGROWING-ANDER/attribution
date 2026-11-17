---
name: gui-attribution-flux
description: "使用 Flux 原生会话分析 GUI 轨迹；输入为 cases 与框架代码，通过现有归因规则逐步提交和校验，无需模型配置、额外凭证或模型 CLI"
---

# Flux 原生 GUI 轨迹归因

用户要求执行 GUI 归因或给出 cases 与框架源码时，完整读取 `stepwise/skills/operator/attribution-bootstrap/SKILL.md` 并执行其检查、准备、Flux 原生派发、提交、最终校验及自动重试流程。最终校验失败时不得停在第一次 invalid；继续执行新 attempt，直到 validated 或重试预算耗尽。用户提供目录、分支和案例范围时遵循其约束。

不要调用 Codex CLI，不要求 model、model_provider、模型网关配置或额外登录态。由 Flux 当前会话或继承当前会话能力的原生子任务执行模型分析，不传模型覆盖参数。

分析方法与判定规则使用冻结的 `stepwise/skills/v3/gui-trajectory-attribution/SKILL.md` 及其引用。工程入口不修改归因算法、Prompt、标签、Schema、分步提交和最终校验逻辑。材料准备成功后继续实际执行原生任务；只生成任务文件不能表示跑通。
