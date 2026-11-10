# 框架职责与模板映射

先查 template-registry.json 的实际快照，再读本节对应框架。这里是按当前项目实现整理的职责，不宣称为各开源项目的唯一官方分类。OSWorld是数据集，不是模块架构。

## Mobile-Agent-v3

template_id=mobile_agent_v3；schema=hierarchy；L1=问题来源、L2=责任模块、L3=具体错误。

- manager：高层子目标、进度、重规划及全任务结束；从实际Manager输出定位错误
- executor：界面原子动作决策；项目里的Operator通常映射executor，需核对角色，不按文件名机械映射
- reflector：动作前后结果评估；比较它的输入、判断及后续采纳
- notetaker：历史事实记录；需看到读写错误，不能因重复操作就自动归记忆
- controller：截图、动作转换与设备执行；需有控制层证据
- 独立grounding日志若属于执行器语义目标到坐标环节，对应 executor/wrong_coordinate；该模板没有单独grounding L2。正确坐标被控制层转换错，才考虑 controller/coordinate_mismatch
- app/app_abnormal：正确操作后App异常；预置内容缺失不硬归此项

Mobile-Agent在桌面OSWorld中可能生成pyautogui或shell命令。定位生成代码的真实模块，说明命令错误性质；动作结构/语法不合法可用executor/invalid_action_format；其余命令问题仍按已有动作类型、参数或执行标签区分，不虚构 command_error 标签，不将所有代码动作都归Controller。

## Agent-S2

template_id=agent_s2_initial_recovery；schema=hierarchy；使用本项目定制模板，不声称为上游官方分类。

- manager：HIGH LEVEL PLAN、(RE)PLANNING、Generated DAG、NEXT SUBTASK；检查子任务是否已把目标改错
- worker：PLAN中的Screenshot Analysis、Next Action、Grounded Action等执行决策。Worker能做局部规划和状态理解，不能仅凭“执行器”一词推定错误都来自Manager
- reflection：Worker内部回顾；根据它实际写的是目标、状态、结果还是恢复建议来解释，不能一律当Mobile A/B/C Reflector
- grounding：RAW GROUNDING MODEL RESPONSE及语义目标到坐标定位；正确目标落错对象、原始坐标本身错误在此查
- controller：Mobile ACI、缩放、输入编码、设备通信；语义坐标正确但实际转换错误才归此层
- app：正确动作后的异常响应

Worker局部done后要继续查Manager是否规划其他子目标。将上游错误传播与下游独立判断分开；只有新证据表明下游应能发现并纠正错误而未处理，才另评Recovery。

## UniPath / UIPath

template_id=uipath；schema=dimensions。内部模板ID采用uipath，文字称UniPath。不指代另一个同名商业RPA产品。

内部模块为 planner、grounder、memory，外部环境来源单列 environment_app：

- planner：读取截图、任务、历史，产生thought/review/plan_action/memory；review不是独立Reflection模块
- grounder：语义动作映射到界面位置，检查输入目标、原始定位与坐标
- memory：跨步存储读写；planner产生的memory内容错误和存储层丢写不是同一原因，按日志区分

Function F1-F9/E1/E2、Recovery R1-R5、Mechanism、Object均是独立维度。不能把Module/Function/Mechanism叫成有父子约束的L1/L2/L3。

手机适配器无单独模板Module：明确F9执行缺口时可 module=null、function=F9、机制/对象按证据填写，mapping_status=gap；在template_gaps说明缺少Adapter/Controller。不把外部执行故障硬归grounder。E1外部App问题使用module=environment_app，actor为app/environment，stage为environment；兼容既有module=null标注。Environment / App不表示模型内部模块，仅用于Initial E1。

Ctrl+A是否可用取决于实际动作契约与当时版本。正确语义动作被旧适配器拒绝，且上游未被明确限制此动作，支持F9；prompt明确禁用而Planner仍输出，可能是Planner动作生成/规划错误；不得仅凭手机任务就说组合键不可用。human_confirmation需看reason及实际拦截分支，不能自动归风险权限。

## 模板使用

选择当前框架对应的注册表条目与模板快照。机器ID和父子关系以快照为准，不凭展示名推测。Mobile-Agent和Agent-S2使用预置L1与五类L2，L3为null；UniPath使用E2、五类预置Mechanism及空Module，Object按实际对象填写或留空。

动作语法或结构不合法：Mobile-Agent为executor/invalid_action_format，Agent-S2为worker/invalid_action_format；UniPath为F6/invalid_action_format，Module根据真实输出来源选择planner或grounder。合法参数值错误仍用原参数标签；正确输出被适配器转换错不归生成模块。

## 职责阶段约束

定位责任时先核对该步上游要求、实际执行决策、定位输出和适配结果。模块名表示当前实现的职责范围，不是错误语义的唯一归属；同一种状态误解或局部规划失误可以来自不同模块，以实际输出为准。责任未知与模板缺少模块分开记录，不猜填最常见模块。

`actor-mapping.json` 的 stage_constraints 按框架规定已知模块允许承担的阶段，external_stage_constraints 规定外部执行和预置环节，label_stage_constraints 约束特定机制。职责不是一对一映射：Worker 可以局部规划，UniPath Planner 可以进行感知；Mobile-Agent-v3 Executor 的目标选择、定位与命令生成不承担 Controller 的适配转换。候选错误、主要根因与恢复根因均需一致。未登记模块不能仅凭名称推断职责，需结合框架材料和模板缺口说明复核。
