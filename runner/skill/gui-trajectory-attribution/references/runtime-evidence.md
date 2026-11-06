# 运行时访问记录

coverage是分析者自报，不是已读取材料的独立证明。可由调用方额外提供Harness采集的只读访问回执，校验器据此交叉检查路径、步骤、字节哈希和成功的工具调用事件。

回执由运行环境采集和保管，不由归因模型生成或补写。输入清单、实际工具调用及成功/失败状态均需保留；文件被列出、只做OCR或生成缩略图不等于全文读取或图片送达。纯API调用应记录实际发送的文本与图像，而不是伪造工具事件。

回执JSON结构：

```json
{
  "producer": "harness",
  "attempt": "与归因输出完全相同的attempt标识",
  "inventory_complete": true,
  "inventory": [
    {"source": "step_1/manager.json", "kind": "log", "step": 1, "sha256": "原始文件字节的SHA256"},
    {"source": "images/frame_1.png", "kind": "image", "step": 1, "sha256": "原始文件字节的SHA256"}
  ],
  "events": [
    {"source": "step_1/manager.json", "operation": "read_text", "success": true, "tool_call_id": "实际调用ID", "content_complete": true, "sha256": "对应原始文件SHA256"},
    {"source": "images/frame_1.png", "operation": "deliver_image", "success": true, "tool_call_id": "实际图像送达调用ID", "content_complete": true, "sha256": "对应原始文件SHA256"}
  ]
}
```

inventory按实际源文件登记，同一文件不因包含多个模块或步骤而重复列项。单步骤文件用step；完整聚合日志用step=null及steps数组，如{"source":"trace.json","kind":"log","step":null,"steps":[1,2],"sha256":"原始文件哈希"}。steps是该文件包含的规范化决策编号，非空、不重复；不能与非空step同时使用。不关联决策步骤的其他日志可仅用step=null。

content_complete只在调用方确认全文或完整原图成功送达时为true；调用成功、源文件哈希匹配仍不足以证明完整送达。截断、只读部分、只发OCR或无法核实完整性时用false或不填，不能计入完整日志阅读或逐张看图。若另记truncated=true，不得同时声称content_complete=true。分块读取可由调用方核对原文范围完整且无截断后发出汇总送达事件，不能仅因所有调用成功就补记完整。旧回执缺少content_complete时只能支持有访问记录，不能支持完整复核声明。图片送达也不证明图像清晰或模型已理解。

路径按attempt相对路径比较，正反斜杠和./作规范化；同一规范化源不得重复登记，不接受绝对路径或上级目录跳转。

每个可用模块日志均须包含在文件索引内；同一步只读Manager、不读其他模块，不能声称该步日志完整。送达事件应由工具或模型调用成功结果生成。拼图必须保留每个原图的映射和送达记录，不可把未包含或不可读图项登记为已核查。

调用方可在模型分析完成后，用独立校验器将归因JSON、当前attempt目录及运行回执交叉核对。访问回执由调用方保管，不要求归因模型自行生成或调用校验工具。

未提供回执时只做自报结构和内部一致性校验，不能把校验通过写成“已验证实际阅读覆盖”。提供回执也不能证明模型理解了内容；它只证明回执与文件、调用记录一致。校验器不认证回执签发方，调用方仍需隔离回执写权限，不能接受模型自造回执作为独立证明。

本Skill提供访问回执规范与校验入口，不包含合成测试；开发测试独立维护，不作为归因输入。规范与入口的存在不表示某个具体Harness已经接入采集，实际接入需由调用方完成并验证。

## 来源类型与步骤对应

清单 `kind` 可为 `log`、`image` 或 `metadata`。对应成功访问事件分别使用 `read_text`、`deliver_image`、`read_metadata`（metadata 也可使用 `read_text`）；事件哈希须与原文件一致。

单步骤来源填写 `step`；跨步骤来源填写 `step: null` 与非空、唯一的正整数数组 `steps`。引用的 `evidence.step` 必须属于来源登记的步骤。聚合日志、复用图片和元数据均遵守此约定，步骤映射须由采集方依据原始记录提供。仅有 `step: null` 而没有 `steps` 时，校验器不能认证该来源的时间对应关系。

最终观测可以登记在最后动作步骤，也可以使用最后步骤加一。前一种登记方式只有在来源清单和引用双方均明确标记 `phase: final_observation` 时，才允许引用为最后步骤加一；不能仅靠模型更改 phase 绕过步骤核对。
