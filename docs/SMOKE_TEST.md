# Prompt r2：传输方式分轮验证

## 当前入口：原生Responses透传

Codex CLI 0.166.0、Skill 0.15.2和启动Prompt保持一致，仅将本地Chat转换改为公司网关的Responses接口。最小连通请求返回HTTP 200、response.completed及指定确认文本，实际模型deepseek-v4-1-flash-261017。

| 案例 | 步骤 | 耗时 | 图片／来源送达 | 最终状态 |
|---|---:|---:|---|---|
| 024 · 斗鱼test_2 | 5 | 6分51秒 | 5/5图，19/19来源 | invalid：追加证据改动原证据 |
| 096 · 携程test_2 | 8 | 4分08秒 | 8/8图，32/32来源 | validated |

共59次请求全部记录为responses_passthrough，protocol_conversion=false；工具定义、工具返回及Responses事件未经过本地Chat转换。日志和图片共51项来源、13张图完整送达。两例均一次独立启动，无最终校验反馈后的模型修复。

携程记录2次读取HTTP流尾部的IncompleteRead；两次均已观察到response.completed，Codex在同一启动中继续，最终通过。不能因此写成无传输异常。

斗鱼5个步骤与全局文件已生成，但revision_004.json改动了原第3条证据，再追加第4条，违反只追加规则；本地基础检查未识别，最终组装器拒绝。该问题属于原有提交约束和模型输出，不是Responses接口拒绝，也不将它计入validated。此轮证明当前接口和完整图文工具调用可用，不证明所有输出都能通过最终校验。

原始记录：独立目录STEPWISE_RESPONSES_DIRECT_20261110，包含gateway_probe.json、smoke_summary.json及two_cases批次。Skill、GT和校验规则未修改。

## 历史记录：Chat转换版

环境：Codex CLI 0.166.0、冻结Skill 0.15.2、stepwise-prompt-r2、公司DeepSeek接口；实际返回模型标识deepseek-v4-1-flash-261017。

选择一条失败GT和一条成功GT的短轨迹，并行运行，各一次独立启动，不向模型提供GT，不做最终校验反馈后的模型修复。

| 案例 | 步骤 | 耗时 | 最终校验 | 图片／来源完整送达 | 局部拒绝 | GT／模型处置 |
|---|---:|---:|---|---|---:|---|
| 024 · 斗鱼test_2 | 5 | 5分16秒 | validated | 5/5图，19/19来源 | 1次 | 失败／失败 |
| 096 · 携程test_2 | 8 | 5分03秒 | validated | 8/8图，32/32来源 | 1次 | 成功／失败 |

完整Skill、八项必需引用与提交协议在实际请求上下文中核对到完整正文；材料送达不等同于模型充分理解。两条均未发生结果字段规范化改动。

局部拒绝分别为非法phase与多余observation_note字段，在同一次启动中收到工具反馈后纠正。因此最终validated为2/2，零局部纠错为0/2。

斗鱼Initial与GT一致：framework → manager → premature_termination，Step 5。携程将“选择最早航班”解释为必须点击／展开航班卡片，虽确认最早航班公务舱价格已显示，仍判失败；冻结GT为成功。处置与GT一致1/2，不将此小样本作为准确率提升结论。

运行记录位于独立实验目录STEPWISE_R2_SMOKE_20261110_two_cases，保留原始启动文本、调用、读取回执、局部错误、组装结果和最终状态。完整60条尚未重跑。
