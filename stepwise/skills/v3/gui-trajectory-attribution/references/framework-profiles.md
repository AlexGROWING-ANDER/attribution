# 架构输入契约

本文件不内置任何具体框架的组件名称、调用顺序、输入输出或职责描述。每次归因只读取调用方为当前 case 选中的 architecture 产物：

- `input_index.json.framework_architecture.markdown_path`：面向阅读的架构摘要。
- `input_index.json.framework_architecture.path`：结构化模块摘要、组件状态、接口、动作常量、调用与继承关系。
- 绑定中的 `mode`：`generated` 表示从固定源码树静态生成；`manual` 表示人工评测概览。
- 绑定中的 `repo_revision`、`source_tree_sha256` 与哈希字段：固定本次使用的架构版本。

## 使用边界

1. 只把 architecture 用于理解组件职责、共享状态、调用边界、动作契约和外部执行边界。
2. `generated` 与 `manual` 使用同一读取契约；不得假设人工概览拥有更高事实优先级。
3. architecture 不是当前 attempt 的证据。不得把其中的源码内容、组件关系或动作定义写入 `evidence`、`source` 或 `locator`。
4. 不得从 architecture 直接推导标签。模板、层级或维度只由当前 `template-registry.json`、选中的 taxonomy 和 `actor-mapping.json` 定义。
5. 若 architecture 与当前日志中的实际模块输出冲突，以当前 attempt 的原始日志、回执和截图判断实际发生行为；architecture 只解释实现边界。
6. 组件名称不是责任结论。责任必须落在该次 attempt 中实际产生错误观察、错误前提、错误动作或错误转换的来源上。
7. architecture 缺失、绑定不完整或哈希校验失败时，记录输入缺口；不得回退到本技能中不存在的框架记忆，也不得凭日志中的展示名猜测架构。

## 读取顺序

先读 Markdown 概览定位模块、入口、共享状态和组件职责，再按需读取 JSON 中的方法签名、字段、源码摘录和调用关系。结构化字段中，`scope` 区分类字段与实例字段，`value` 只表示在 `__init__` 中找到的默认字面量，`written_in`／`read_in` 表示记录到的方法读写位置；它们描述实现，不表示本次 attempt 的实际状态。仅在当前争议需要时深入具体组件，避免把 architecture 的全部实现细节复述进最终结果。
