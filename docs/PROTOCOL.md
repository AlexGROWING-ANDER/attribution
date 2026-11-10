# 逐Step提交与校验

## 输入与职责

启动Prompt提供当前任务身份及资源入口。模型原生加载归因Skill，读取材料索引、单步Schema、全局Schema和提交协议。

| 文件 | 用途 |
|---|---|
| `stepwise/prompts/stepwise_en.txt` | 实际启动Prompt模板 |
| `stepwise/protocols/submission_protocol.md` | 模型实际读取的提交协议 |
| `stepwise/skills/v3/gui-trajectory-attribution/SKILL.md` | 分析流程、证据要求与归因规则入口 |
| `docs/examples/prompt_case001.txt` | 当前英文Prompt示例 |
| `docs/examples/prompt_case001_中文.md` | Prompt与提交协议的中文说明 |

全部网站图片逐张送达，每步日志完整读取。来源编号和图库序号本身不证明图片时序。分析方法、主要根因选择及责任模块判断由Skill规定，提交协议负责工具和文件接口。

## 提交过程

```text
准备Skill与当前attempt材料
  → 完整读取图片和日志
  → 按步骤保存局部判断，检查字段、步序和来源
  → 提交全局归因，检查补充证据是否保留原证据前缀
  → 本地完成检查：步骤、文件、补充证据及引用
  → 程序组装，绑定任务身份、来源和覆盖记录
  → 最终严格校验
  → status.json：validated／invalid／runtime_failed
```

通过PowerShell调用`io.ps1`传递单步小JSON，按数字顺序保存。单步记录包含`step`、`verdict`、`summary`、`evidence`；`verdict`是局部判断，不直接等于主要根因。

新读取的直接观察可另存为`revision_NNN.json`。原始步骤、verdict和证据前缀保持不变，补充内容追加在后。全局提交和本地完成检查均会检查这项约束，拒绝时返回具体冲突位置。

模型不覆盖已保存的基础步骤。局部拒绝保留记录，纠正预算按提交协议执行。最终严格校验由调用方执行，不向模型反馈结果回修。

分片合法不等于整体结果合法；最终检查还会核对引用、责任模块、标签、恢复机会、目标和处置的一致性。`validated`表示程序检查通过，归因准确率需另与人工GT比较。

## 原生Responses传输

入口使用公司网关的Responses接口。本地记录器保留工具定义、消息结构、工具返回及响应事件，不转换为Chat Completions，不重建Responses事件。记录器恢复清单约定的图片字节，并将`codex-auto-review`明确路由到同一DeepSeek模型。

响应按原字节流转给CLI，审计提取HTTP状态、模型名、usage及完成事件。收到完整`response.completed`后，该次请求携带的材料才记为完整送达。上游HTTP错误保留原状态码；流中途出错则关闭当前连接。请求和流重试由Codex配置控制。

传输审计文件为`bridge_audit.json`，其中`transport=responses_passthrough`、`protocol_conversion=false`。送达回执表明材料已进入请求，不等于模型已经正确理解。
