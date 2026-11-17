---
name: attribution-bootstrap
description: "在 Flux 中从 GUI cases 与框架源码执行归因：准备可校验清单和独立案例工作区，以 Flux 原生子任务读取归因 Skill、截图和日志，逐步提交并由父任务严格校验；校验失败自动携带错误创建新 attempt 重试；不需要模型配置或模型 CLI"
---

# Flux 原生 GUI 归因执行

使用 Flux 当前会话或原生子任务执行模型分析；Python 只负责数据适配、材料冻结、提交检查、状态管理、结果组装及最终校验。不要调用 Codex CLI、其他模型 CLI、模型 API，或读取模型配置与凭证。不要求用户提供模型名、provider 或网关配置。

## 输入及边界

- 必需输入：cases 路径、框架代码路径、新输出目录。URL 先按当前用户授权读取到本地，遵循平台仓库准备规则；不能获得材料时报告缺口。
- 支持 `Agent_S2`、`Mobile_Agent_v3`、`uipath`，目录和数据包名不区分大小写，兼容 `ui_path` 等旧名称。存在多个匹配目录则报告歧义。
- annotations 不是必需输入。缺失时按 cases 目录发现全部案例，并从原始控制台日志、Mobile `manager.json` 或 UiPath `task_meta.json` 提取任务指令；存在时用于补充 identity，Agent_S2/UiPath 仍尊重显式 `excluded`。
- 模型只读取本案例工作区里的材料、归因 Skill、协议与架构概览。不读取 GT、标注结论、其他案例或历史预测。
- 架构概览只帮助理解框架，不能作为 attempt 证据，不能据此选择标签或错误步骤。
- 原生 Flux 子任务继承平台权限。每个 case 独立工作区、独立上下文和提交记录；这不声称额外创建了物理文件系统沙箱。
- 单案例分析方法、Prompt、标签、Schema、checkpoint/分步提交协议和最终校验逻辑使用原有冻结版本。

## 1. 检查与准备

在技能所在代码目录执行：

```bash
python3 data/bootstrap_attribution.py inspect --cases /path/to/cases --code /path/to/codes
python3 data/bootstrap_attribution.py prepare --cases /path/to/cases --code /path/to/codes --out /path/to/new-prepared --preflight
```

核对 framework、案例范围、固定架构绑定及清单哈希。预检只验证材料及运行文件，不能表示归因完成。未知数据布局、证据缺失或哈希不匹配时停止相关案例，不猜造输入。

## 2. 准备原生任务

```bash
python3 data/bootstrap_attribution.py run --prepared /path/to/new-prepared --jobs 2 --wall 3600
```

需要部分案例时追加 `--case-id CASE_ID`；恢复同一 scope 使用 `--resume`。读取批次中的 `dispatch.json`、`batch_state.json` 和每条任务的 `flux_task.md`。状态 `ready_for_flux` 仅表示材料可执行。

**用户要求跑归因时，继续执行后面的原生任务；不要生成任务文件后就结束。** Python 命令本身不能调用当前 Flux 工具，不虚构 Flux REST 接口或命令行 SDK。

## 3. 执行案例

每次派发新原生子任务前检查批次目录 PAUSE；存在时保留未执行项并停止新派发，已完成项仍可组装校验。核对当前平台真实提供的原生子任务工具及 schema。可用时按 case 派发，最大活跃数取 jobs、5 和 cgroup CPU 配额的最小值；缺少原生子任务工具时在当前 Flux 会话按 case 顺序执行。

- 子任务只传一条案例的 `flux_task.md` 路径、工作区和输出范围。使用独立上下文：V1 明确 `fork_context=false`，V2 明确 `fork_turns="none"`；不传模型或 provider 字段。
- 子任务标题使用差异化短标题，例如“单案例原生归因执行与逐步提交”。第一行写“标题：…”，明确任务、写入范围、产物与完成条件。
- 子任务完整读取自己的 `flux_task.md`、原始 `prompt.txt`、工作区归因 Skill 及必需引用，先完成 checkpoint 提交，再按 step_index.json 顺序读取材料和通过 io.py 提交。
- 禁止 child 自行运行最终严格校验器、修改 Skill、helper、材料、checkpoint 或已保存的 Step；不增加其他模型调用。
- child 必须先把本地 `io.py check`/`io.ps1 -Mode check` 跑到通过；被拒时按具体错误修正并复查，最多三个修正循环。不能带着本地失败回复 DONE。
- 若任务由 `run_batch` 自动重试生成，child 必须先完整读取当前 attempt 的 `retry_context.json`，只把其中错误摘要用于避免重复错误；不得读取上一 attempt 的 result、判断文件或其他历史预测，必须从本 attempt 原始材料重新分析。父任务派发时也只传当前 attempt，不传旧 attempt 工作区。
- 等待原生子任务时使用平台工具最大等待参数（V1 为 `timeout_ms=3600000`）；工具返回运行中时持续等待原调用，不因暂时无输出而重复派发。
- 若 child 报告材料或工具阻塞，保留状态和证据；不能把缺失输出补成完成。

启动文档（任务、原 Prompt、Skill/引用、Schema、输入索引及架构概览）用原生 exec_command 逐文件 cat 全文读取，不经轨迹票据门面，在读取轨迹之前完成。

读取日志和截图通过 `stepwise/engine/flux_execution.py read` 获取材料及 ticket。完整文本工具返回后确认 text ticket；图片路径返回后还须逐张调用原生 `view_image`，实际图像展示成功后才能确认 image ticket；支持 detail 参数时明确使用 original 并转发原始图像。具体命令见每条 `flux_task.md`。

回执 producer 保持原有 `harness`，新增 `confirmation_producer=flux_agent` 和 `execution_backend=flux_native` 标识，记录的是 agent 对原生工具成功展示的确认；不声称抓取了上游 HTTP 请求、平台完整 trace 或模型理解情况。ticket 是本地关联编号，不能冒充平台工具编号。文本截断、图片未展示、工具失败时不得确认。派生视图必须经原 io.py derive 生成，再通过 read/view_image/acknowledge 登记；不能计入原图库覆盖。

## 4. 父任务组装、校验与自动重试

child 回复 DONE 且本地 check 通过后，由父 Flux 任务对批次执行：

```bash
python3 stepwise/run_batch.py --manifest MANIFEST --out BATCH --finalize --resume
```

父任务执行原有结果组装、确定性规范化和严格校验。若结果是 `invalid`、`runtime_failed` 或 `infra_failed`，批次不会把第一次失败当作最终交付：它保留旧 attempt，创建下一条 `attempt_NN`，把上一轮 `error`、`schema_errors`、`validation_errors` 和针对性修复提示写入新 attempt 的 `retry_context.json`，更新 `dispatch.json` 后进入 `awaiting_retry`。启动前超时/失败、binding/status 不可读或身份不符也走同一条有界重试路径，`launch_failure.json` 保留启动错误。随后必须执行 dispatch 中的新任务，并再次 finalize；重复直到 `validated` 或预算耗尽。

`--finalize` 只有所有案例均为 `validated` 时返回 0。`awaiting_retry`、`ready_for_flux`、`unresolved_after_retries` 或任何未完成状态均返回非 0，不能把“已生成重试任务”误报为批次完成。

默认 `--max-attempts 3`，即首次 attempt 加最多两次重试；需要其他预算时在创建批次和每次 resume/finalize 时传相同值。只有预算耗尽仍失败才标记 `unresolved_after_retries`，此时应明确报告未解决，不能把第一次 invalid 当正常交付。所有 attempt 目录都保留，旧 attempt 不覆盖、不删除。

重试任务完成后，可执行 `python3 stepwise/run_batch.py --manifest MANIFEST --out BATCH --collect --resume` 只读回读状态。只有 `validated` 计为程序校验通过；`ready_for_flux` 表示新任务待执行，`unresolved_after_retries` 才表示重试预算耗尽。`validated` 不表示与人工 GT 一致。

## 输出

每个 case 保留独立工作区、原始 Prompt、input_binding.json、flux_session.json、读取确认票据、checkpoint、Step、global_summary、io_events、access_receipt、归一化审计、result.json、attribution_result.json 和 status.json；重试 attempt 另保留 retry_context.json。保存原生子任务标识及其完成结果用于追溯，不能从一句 DONE 重构缺失材料或伪造访问事件。

原生子任务的 reasoning 事件若由平台以 session JSONL 暴露，`finalize` 会按 attempt 路径自动查找对应 subagent session 并归档；若平台未自动落盘，可使用 `python3 stepwise/engine/reasoning_chain.py --attempt ATTEMPT --discover-attempt --module gui-trajectory-attribution`，已知 thread ID 时也可传 `--thread-id`。该命令只保存平台实际发出的 reasoning 事件和来源行号，生成 `reasoning_chain.jsonl`、`reasoning_chain.md` 与 `reasoning_chain.meta.json`；不得从不完整摘要或 DONE 回复补造隐藏思维链。平台未暴露 reasoning 时保留 `available=false`。

`attribution_result.json` 与校验状态解耦：`validated` 时取规范化后的 `result.json`；`invalid` 时仍保留模型已给出的判定、完成度、goals、主因、L1/L2/L3 和 synthesis，并写明校验错误与来源。若正常组装失败，使用已提交的 Step 与 `global_summary.json` 生成 best-effort `result.json`；即便只能降级组装，也必须保留真实 labels 和 coverage 缺口，并通过 `result_provenance.json` 标记 `validation_passed=false`。批次同时生成 `attribution_results.json` 汇总全部案例，包括 invalid；不得因为校验失败而从归因输出中删除模型已给出的结果。该校验后结果不冒充 `validated`，也不能用于覆盖原始提交。

汇总说明实际派发、attempt 数、validated/unresolved/待执行数量及证据覆盖，未完成项保持未完成。不得把第一次 invalid 当作最终结果；必须继续执行自动生成的重试任务直到 validated 或明确报告预算耗尽。离线工程检查和离线历史 GUI 材料归因不冒充业务 API/GUI 测试，不制造业务测试轮次。
