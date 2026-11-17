# Skill 使用说明

入口为仓库根 SKILL.md。Flux 完整读取 `stepwise/skills/operator/attribution-bootstrap/SKILL.md` 后执行检查、准备、原生任务派发、读取确认、逐步提交和父任务最终校验。该文件是执行流程的唯一操作说明。

输入仍为 cases、框架源码和新输出目录；无需配置模型、provider、网关或登录文件。模型侧使用原冻结归因 Skill，规则不变。

Python 命令生成 dispatch.json 和 flux_task.md；Flux 原生会话才执行推理。原生子任务可用时使用独立上下文并继承当前模型能力，不传模型字段；不可用时由当前会话顺序执行。最终校验失败时批次默认最多创建 3 个全新 attempt，并把上一轮精确校验错误写入新 attempt 的 retry_context.json；必须继续执行新任务，直到 validated 或预算耗尽。finalize 只有全量 validated 才返回 0，awaiting_retry 表示还需继续执行。材料准备不等于归因完成。

回执为 agent 对原生工具成功展示的确认，不能声称是上游 HTTP 验证。仅在实际完整读取/显示后确认；未完成项保留真实状态。截图、日志和 Step 数量按完整清单核对，原最终校验器仍检查源哈希、完整送达、引用和输出规则。
