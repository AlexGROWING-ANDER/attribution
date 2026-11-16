---
name: attribution-bootstrap
description: "在 Flux 中从 GUI cases 与框架源码执行归因：准备可校验清单和独立案例工作区，以 Flux 原生子任务读取归因 Skill、截图和日志，逐步提交并由父任务严格校验；不需要模型配置或模型 CLI"
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
- 等待原生子任务时使用平台工具最大等待参数（V1 为 `timeout_ms=3600000`）；工具返回运行中时持续等待原调用，不因暂时无输出而重复派发。
- 若 child 报告材料或工具阻塞，保留状态和证据；不能把缺失输出补成完成。

启动文档（任务、原 Prompt、Skill/引用、Schema、输入索引及架构概览）用原生 exec_command 逐文件 cat 全文读取，不经轨迹票据门面，在读取轨迹之前完成。

读取日志和截图通过 `stepwise/engine/flux_execution.py read` 获取材料及 ticket。完整文本工具返回后确认 text ticket；图片路径返回后还须逐张调用原生 `view_image`，实际图像展示成功后才能确认 image ticket；支持 detail 参数时明确使用 original 并转发原始图像。具体命令见每条 `flux_task.md`。

回执 producer 保持原有 `harness`，新增 `confirmation_producer=flux_agent` 和 `execution_backend=flux_native` 标识，记录的是 agent 对原生工具成功展示的确认；不声称抓取了上游 HTTP 请求、平台完整 trace 或模型理解情况。ticket 是本地关联编号，不能冒充平台工具编号。文本截断、图片未展示、工具失败时不得确认。派生视图必须经原 io.py derive 生成，再通过 read/view_image/acknowledge 登记；不能计入原图库覆盖。

## 4. 父任务组装与校验

child 回复 DONE 且本地 check 通过后，由父 Flux 任务执行：

```bash
python3 stepwise/run_case.py --manifest /path/to/new-prepared/inputs/run_manifest.json --case CASE_ID --out /path/to/new-prepared/batch/runs/CASE_ID/attempt_01 --finalize
```

父任务执行原有结果组装、确定性规范化和严格校验一次。最终校验失败后不把结果反馈给模型进行回修，不覆盖已完成尝试。

对批次执行 `python3 stepwise/run_batch.py --manifest MANIFEST --out BATCH --collect --resume` 回读状态；批次 `--finalize --resume` 可按真实提交收口。只有 `validated` 计为程序校验通过；`invalid`、`runtime_failed`、`infra_failed`、未派发和 `ready_for_flux` 分别统计。`validated` 不表示与人工 GT 一致。

## 输出

每个 case 保留独立工作区、原始 Prompt、input_binding.json、flux_session.json、读取确认票据、checkpoint、Step、global_summary、io_events、access_receipt、归一化审计、result.json 和 status.json。保存原生子任务标识及其完成结果用于追溯，不能从一句 DONE 重构缺失材料或伪造访问事件。

汇总说明实际派发、完成、validated/invalid/阻塞数量及证据覆盖，未完成项保持未完成。离线工程检查和离线历史 GUI 材料归因不冒充业务 API/GUI 测试，不制造业务测试轮次。
