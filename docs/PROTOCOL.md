# 逐Step提交与校验

## 输入与职责

启动Prompt提供当前任务身份及资源入口。模型原生加载归因Skill，读取材料索引、checkpoint Schema、单步Schema、全局Schema和提交协议。

| 文件 | 用途 |
|---|---|
| `stepwise/prompts/stepwise_en.txt` | 实际启动Prompt模板 |
| `stepwise/protocols/submission_protocol.md` | Windows模型实际读取的提交协议 |
| `stepwise/protocols/submission_protocol_posix.md` | macOS/Linux模型实际读取的提交协议 |
| `stepwise/skills/v3/gui-trajectory-attribution/SKILL.md` | 分析流程、证据要求与归因规则入口 |
| `docs/examples/prompt_case001.txt` | 当前英文Prompt示例 |
| `docs/examples/prompt_case001_中文.md` | Prompt与提交协议的中文说明 |

全部网站图片逐张送达，每步日志完整读取。来源编号和图库序号本身不证明图片时序。分析方法、主要根因选择及责任模块判断由Skill规定，提交协议负责工具和文件接口。

macOS/Linux的Python helper还提供受控派生视图：只允许从已登记原图做裁剪和固定比例缩放，输出必须位于`derived_views/`且由helper登记。派生视图只辅助局部放大，不替代原图送达，不改变来源编号、图库数量或覆盖统计；桥接层按父图送达字节和几何参数重算校验。Windows仍使用原有PowerShell协议，不开放该helper模式。

## 提交过程

```text
准备Skill与当前attempt材料
  → 依据指令、身份和任务口径提交checkpoint计划（1—8项）
  → 完整读取图片和日志
  → 按步骤保存局部判断，检查字段、步序和来源
  → 提交全局归因，检查required checkpoint是否原样进入goals、补充证据是否保留原证据前缀
  → 本地严格检查：步骤、文件、补充证据及引用；被拒时最多三个修正循环
  → 程序组装，绑定任务身份、来源和覆盖记录
  → 最终严格校验
  → validated，或自动创建带 retry_context 的新 attempt
  → 重试预算耗尽才记录 unresolved_after_retries
```

Windows通过PowerShell调用`io.ps1`，macOS/Linux调用`io.py`传递JSON，按数字顺序保存。macOS/Linux启动时会把工作区协议中的`python3`固定为执行器实际使用的Python绝对路径，避免沙箱内`PATH`落到不可用的系统解释器。两条路径都要求先提交`checkpoint_plan.json`：计划只能来自原始指令、身份和任务口径，提交事件绑定文件哈希；未经该提交，step、global、check（以及POSIX derive）都会被拒绝。最终required checkpoint必须与`goals`中的`id/object/requirement`一致。该门禁不阻止直接读取原始文件，实际顺序另写入组装审计。两条路径执行相同的字段、步序、来源和revision前缀检查，并在`global`与`check`再次执行严格JSON Schema校验。POSIX的`check`还会根据confirmed delivery ticket重建运行期receipt，复用canonical assembler和最终`validate_result`执行本地严格语义预检；未送达的图库图片、标签层级冲突、actor/stage冲突以及完成状态与二值decision不一致都会在本地被拒绝。Windows helper同步拦截schema额外字段、未送达图库图片和未知引用，完整语义校验仍由最终化阶段执行。`satisfied`或`unsatisfied`目标必须至少引用一个绑定到图片来源的evidence ID，仅有日志或模块自述时拒绝并提示保留`unknown`。单步记录包含`step`、`verdict`、`summary`、`evidence`；`verdict`是局部判断，不直接等于主要根因。

checkpoint是验收覆盖脚手架，不是证据：不得写入evidence ID，不能单独证明错误，也不能直接选择层级或责任标签。任务执行中由直接证据发现的额外验收项可以继续加入`goals`。

新读取的直接观察可另存为`revision_NNN.json`。原始步骤、verdict和证据前缀保持不变，补充内容追加在后。全局提交和本地完成检查均会检查这项约束，拒绝时返回具体冲突位置。

模型不覆盖已保存的基础步骤。局部拒绝保留记录，纠正预算按提交协议执行。最终严格校验由调用方执行；若仍失败，`run_batch` 不把第一次 invalid 当作最终交付，而是保留原 attempt，创建下一条 `attempt_NN`，把具体 schema/validation 错误写入 `retry_context.json` 后重新派发。默认总共最多 3 个全新 attempt；只有预算耗尽才记录 `unresolved_after_retries`，且旧 attempt 不被覆盖。绑定/状态损坏和启动前失败也计为一次 attempt，不能无限复用同一失败目录；`--finalize` 仅在全量 `validated` 时返回 0。

分片合法不等于整体结果合法；最终检查还会核对引用、责任模块、标签、恢复机会、目标和处置的一致性。`validated`表示程序检查通过，归因准确率需另与人工GT比较。

## 推理链路归档

每次attempt可将模型运行时实际发出的reasoning事件归档到`reasoning_chain.jsonl`，同时生成便于阅读的`reasoning_chain.md`和`reasoning_chain.meta.json`。`reasoning_chain.jsonl`只按实际事件顺序保存文本及来源行号，不补写、不合并模型没有发出的隐藏推理；Codex同时写入`event_msg`和`response_item`的同一reasoning条目会按event ID去重。`attribution_result.json.reasoning_chain`只保存归档文件索引和计数。

旧CLI执行器在写出`stdout.jsonl`后自动归档。Flux原生任务在`finalize`时按attempt路径查找对应subagent session并自动归档；若平台未落盘session，可在拿到session JSONL或thread ID后补执行：

```bash
python3 stepwise/engine/reasoning_chain.py --attempt ATTEMPT_DIR \
  --discover-attempt --module gui-trajectory-attribution
```

没有可用reasoning事件时仍写出空JSONL和meta，并将`available`记为false；这表示运行时未暴露该内容，不能从最终回答或归因摘要反推成“完整思维链”。

## 原生Responses传输

入口由 Flux 原生会话或原生子任务执行推理，不启动模型 CLI，也不读取模型配置。材料通过原生文本读取和 view_image 展示；读取确认门面保存 agent 确认回执，不声称上游 HTTP 请求已被独立验证。原有 checkpoint、逐步提交、组装、引用与最终校验规则保持不变。

响应按原字节流转给CLI，审计提取HTTP状态、模型名、usage及完成事件。收到完整`response.completed`后，该次请求携带的材料才记为完整送达。上游HTTP错误保留原状态码；流中途出错则关闭当前连接。请求和流重试由Codex配置控制。

传输审计文件为`bridge_audit.json`，其中`transport=responses_passthrough`、`protocol_conversion=false`。送达回执表明材料已进入请求，不等于模型已经正确理解。

派生视图送达单独写入`derived_view_audit.json`，记录父图`source_id`、父图源/投递哈希、裁剪框、缩放比例、输出尺寸和派生字节哈希。`access_receipt.json`不把派生视图计为图库图片。
