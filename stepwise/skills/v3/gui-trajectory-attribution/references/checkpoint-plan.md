# Checkpoint 计划

Checkpoint 计划在阅读轨迹证据之前建立，用来固定“原任务需要哪些可验收状态”。它约束后续 `goals` 的覆盖范围，但不是证据，也不是动作清单。

## 生成边界

- 只使用原始指令、当前身份（框架、平台、任务约束）和 `task-policy.json`。不得从日志、截图、执行回执、失败现象或已有动作序列反推 checkpoint。
- 描述任务级状态：对象、数量、排序/筛选、交付或必要前置。一个 checkpoint 应能在任务结束时判断“达成 / 未达成 / 未知”。
- 使用 1–8 个 checkpoint；不要拆成逐步 GUI 操作、模型内部子步骤或预期要点击的控件。
- 可以识别必要前置条件和任务约束，但不要把正常登录、已有输入或任务明确需要的个性化状态自动升级为异常。
- 计划提交后保持原文。后续证据可以追加额外 `goals`，但不能改写 required checkpoint 来迎合观察结果。

## 结构

```json
{
  "schema": "case-checkpoint-plan-v1",
  "case_id": "case_001",
  "basis": ["instruction", "identity", "task_policy"],
  "checkpoints": [
    {
      "id": "CP1",
      "type": "goal",
      "object": "热门搜索中的一首歌",
      "requirement": "歌曲被选中并实际开始播放",
      "required": true,
      "depends_on": [],
      "acceptance": "播放控件、播放页或执行回执直接证明，不接受 Agent 自述",
      "evidence_expectation": ["screenshot", "execution_result"]
    }
  ]
}
```

- `id` 使用 `CP1`、`CP2` 等稳定编号。
- `type` 取 `goal / precondition / constraint / delivery`。
- `required=true` 表示最终 `goals` 必须存在同 `id`、同 `object`、同 `requirement` 的验收项。
- `depends_on` 只表达 checkpoint 之间的前置关系，不表示动作先后，也不得成环。
- `acceptance` 描述什么材料足以判断状态；`evidence_expectation` 是预期材料类型，不是证据引用。
- 不要添加 `evidence_ids`、`source_id`、截图路径、日志路径或任何已观察事实。

## 与最终归因的关系

判定每一步时，可以参照 checkpoint 理解当前子目标，但不要求每个 step 都引用某个 checkpoint。最终 `goals` 是 checkpoint 的验收投影：required checkpoint 必须保留原 `object` 和 `requirement` 并填写 `status` 与证据；任务中额外发现且由直接证据支持的验收项可以继续追加。组装器会为每个 goal 生成 `steps`，由所引用证据的实际 step 与非空 `first_seen_step` 汇总，并生成覆盖这些步骤的闭区间 `step_range`；模型不需要在 checkpoint 计划中预填具体步号。

Checkpoint 未达成只说明验收状态，不自动证明模型有错：它可能来自原任务前置条件、外部阻塞、模型决策或动作转换。Initial、Recovery、错误步骤和责任模块仍按完整证据链判断，且必须找到首次产生错误前提、判断、动作或转换的真实节点。Checkpoint 及其状态不能直接充当证据，也不能反向决定标签层级。
