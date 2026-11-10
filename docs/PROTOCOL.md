# 逐Step提交与校验

模型加载Skill引用、材料索引、单步Schema和全局Schema。全部网站图片须逐张送达，每步日志须完整读取；不能用图片序号直接推定操作步号。

按真实步骤数字顺序输出 `step`、`verdict`、`summary`、`evidence`。`verdict`为reasonable/error/uncertain，是局部判断，不直接等于主要根因。证据标明来源编号、动作前后时序、文件内部位置和直接事实。

图片、日志及步骤齐全后，再整体分析目标、污染、预置问题、候选原因、Initial和Recovery。先建立错误语义和因果关系，再判断责任模块，最后映射模板；不机械取最早异常，步数耗尽不自动作为根因。

## 提交过程

```text
准备Skill与当前attempt材料
  → 全量读图／日志
  → 每Step判断并保存，基础检查与局部反馈
  → 全局归因
  → 检查全部步骤与必需文件
  → 程序组装、绑定身份／来源／覆盖
  → 最终严格校验一次
  → status.json：validated／invalid／运行失败
```

这轮 `io.ps1` 仍通过PowerShell传递小JSON，不是后来typed_v3的原生MCP提交方式。模型不能自由覆盖旧步骤，局部拒绝会保留记录。基础检查允许一次针对修正，不提供最终严格校验后的模型回修。

允许根据新读取的直接证据另存追加修订，保留原步骤及证据前缀，不静默修改原来的局部结论。

组装器可在生成result.json之前因缺件或非法引用失败。分片分别合法，也可能在整体检查中出现责任、标签、恢复机会、目标和处置不一致。

## 导出与原批次的关系

- 13份核心引擎和依赖文件先比对原冻结SHA256，再导出
- Skill v3的17份文件保持原始字节
- 启动Prompt集中于stepwise/prompts/stepwise_en.txt，仅引用Skill、任务和资源；stepwise_support_fast准备提交助手与独立submission_protocol.md，不再在Prompt中重复归因流程
- 原生引擎把写死的PowerShell沙箱运行时位置改为配置项，并在状态中记录实际校验器哈希，替代过期的固定文字
- 新入口从参数读材料清单、从环境读网关配置，关闭宽松合并
- 保留原引擎的失败分类，移除包装层对该分类的多余覆盖
- 实际校验器CLI默认读取导出包内规则，也可显式传入references目录；校验逻辑不变
- 增加确定性规范化及编译字段变化审计，保留原处理规则及原始预测
- 新批跑器每case一个独立进程，最多16路，不覆盖已有结果，不读取GT或自动编辑Skill

仅导出原生逐Step路径所需依赖。历史引擎中未暴露的单次大JSON、回修和研究编辑函数不属于新入口，不将它们当成受支持命令。

RELEASE_MANIFEST记录本次导出的文件；source-manifest记录原文件哈希和可移植调整，两者不混称同一快照。

## 原生Responses传输

当前入口使用公司网关的Responses接口。本地送达记录器保留工具定义、消息结构、工具返回及响应事件，不再将请求改成Chat Completions，也不重建Responses事件。只恢复清单约定的图片字节，并将codex-auto-review模型名明确路由到同一DeepSeek模型。

响应按原字节流转给CLI，审计仅提取HTTP状态、模型名、usage及完成事件。只有收到完整response.completed后，才把该次请求携带的材料记为送达；响应中断不计完整送达。上游HTTP错误保留原状态码，流中途出错则关闭当前连接，不再附加第二个HTTP响应。不自动回退Chat接口；请求和流重试由Codex配置控制。

历史名称bridge_audit.json继续用于兼容已有审计工具。当前记录中transport为responses_passthrough、protocol_conversion为false，不能因文件名将它理解为协议转换桥。
