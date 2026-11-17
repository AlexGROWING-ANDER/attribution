# 输出约定

机器输出遵守 output.schema.json，UTF-8 JSON，不包含Markdown围栏。所有顶层字段必填；真正未知使用约定枚举/null，不用空字符串或0伪装标签/步骤。本契约只规定归因结果的结构与证据要求，展示及外部系统写入由调用方另行处理。

正式JSON以已确认的原始指令、attempt、框架及注册模板为前提。缺少这些身份信息时先返回输入缺口清单，不为满足枚举而猜选模板或编造attempt；这份补充材料请求不是schema结果。身份完整但日志或图片缺失时，仍可输出 completion=unknown、decision=fail 及覆盖缺口的JSON。

## 规范存储与显式提交视图

本文件与output.schema.json定义规范结果。调用方可以提供由该规范确定性导出的提交schema，并明确哪些身份/清单字段由程序绑定；此时模型按显式提交schema输出唯一结果，由调用方编译回规范结构。不能自行省略规范字段，也不能无视明确的提交视图输出两套格式。判断口径和证据要求不因表示视图改变；程序性绑定不可替代事实归因。

## 身份与状态

case：framework 使用当前清单绑定的机器 ID；dataset 独立填写实际数据集；task_id、attempt、适配器版本单列，未知版本填 unknown。template_id、template_sha256、policy_id 和 skill_version 从模板注册表读取，不能凭空生成。

outcome：completion是任务客观完成情况；decision只有 success/fail，表示是否完成。只有 completion=success、全部 goals satisfied、覆盖完整且完成证据充分时使用 success；其余情况使用 fail。basis说明原因，evidence_ids指向支持该判定的原始材料。

contamination、preconditions 分别记录，不能互相覆盖。preconditions 使用当前模板预置类型的机器 ID；项目扩展分类不声称为上游官方标签。每项包含 Agent 如何识别/处理，能区分环境不足与应对错误。污染来源未知写明未知，reset_requirements 仅列目标对象需要恢复的状态，不建议清空全部账号数据。

goals覆盖来自原始指令及公共任务口径的每个验收项，包括数量、筛选与额外操作约束；不将模块自行增加的计划当作验收项。判为unsatisfied时在requirement或解释中交代要求来源，不增加额外schema字段。可按相同对象的不同要求拆项。baseline_state与this_attempt_actions区分起点与本次完成。成功时每个验收项都需satisfied及证据；任务开始已满足则另按污染规则处置。

提交前建立的 checkpoint 计划是 goals 的覆盖基线。每个 required checkpoint 都必须在 goals 中以相同 id、object 和 requirement 出现，再补充实际 status、first_seen_step、baseline_state、this_attempt_actions 与 evidence_ids；证据发现的额外验收项可以追加。计划本身不是证据，不能写入 evidence_ids 或作为标签选择依据；checkpoint 未达成也不直接推出模型责任。

最终 `goals[].steps` 由调用方在组装时计算：汇总该 goal 所引用证据的实际 step，并并入非空 `first_seen_step`。`goals[].step_range` 是同一组步骤的闭区间 `[最小步, 最大步]`，无可绑定步骤时为 `null`；区间内未被引用的中间步骤不等同于已有证据。模型不需要也不应猜测具体步号；这两个字段只表示验收项在本次 attempt 中涉及的步骤范围，不改变证据、错误步骤或责任归因。

## 汇总结论与标签映射

synthesis是全部逐Step判断完成后的全局综合结论，必须在确定error_candidates主次、填写attribution和模板标签前作为完整自然语言结论写出。它不替代证据字段，也不要求输出私有思维过程。

summary只写一段跨步骤综合判断，通常150—300字：概括哪些验收目标失败或成功、决定性事实、主因节点及责任来源、恢复结论和仍未闭合的证据。不要逐条复述error_candidates、attribution或recovery_opportunity；这些字段已保存详细论证。

synthesis.summary只描述与当前标签基准无关的归因内核，不写L1/L2/L3、Module/Function/Mechanism/Object或模板标签ID。先写summary固定事实链、责任来源和主因，再映射attribution.labels；模板或层级基准变化时只重做标签映射，不因新标签反向改写summary中的证据和因果结论。primary_selection可以引用synthesis，但不得与其矛盾。

## 覆盖、步骤与证据

coverage.available_steps是按当前分析规范化后的一基决策编号，total_steps是其数量；原始日志从0开始时在证据locator中注明原编号映射，不改源文件。允许缺步，不假设编号连续。

log_steps_read只填已读完全部可用模块日志、动作与回执的步骤，不是只读Manager或摘要。screenshot_sources_available列出当前attempt全部图片来源相对路径，screenshots_index_complete表示是否已核全索引；screenshots_available等于清单数量。screenshots_viewed只填真正逐张看清的图片，不能因为文件被列出就加入。scan_complete=true须完整日志已读、图片索引完整且清单内每张图片已看，无缺失或截断。缺失、不可读、索引不全须记入coverage缺口；图像文件编号与步骤编号分开记录。

证据列表每项：id唯一；kind区分截图、模块输出、动作、执行结果、元数据；phase标明操作前/后、决策、执行或最终观察；source为attempt内相对路径；locator为行号/JSON路径/图片区域。step指所关联的规范化动作节点，独立元数据可null；最后独立观测可用下一编号且phase=final_observation。网页或嵌入图像若没有本地原路径，由调用方保存原始输入并给出对应source，不编造文件。

截图的observation只写直接可见内容。模块日志的observation可以写“该模块称已完成”，不能将其冒充目标完成事实。所有evidence_ids必须引用已登记的ID。

process为覆盖关键过程的多行记录，各行start_step/end_step对应真实步骤，summary写动作和变化，附证据ID。过程记录应覆盖开始、目标选择、关键执行、异常/恢复、结束，不用一行主因代替全过程。若scan_complete=true，过程区间应覆盖全部可用步骤；可以合并正常连续阶段。

evidence.phase的七个合法取值仅为before_action、after_action、decision、execution、metadata、final_observation、unknown，表示证据发生时序。verification属于错误stage词表，不能跨填phase。模块输出kind与时序phase也不同，例如反思模块的判断可发生在decision阶段，但其内容仍是module_output，不能因此重标为外部execution_result。

原始source/source_id是归因引用锚点。经过拼接或分段的阅读页只提供原来源的视图；locator须指向所选原来源内的字段/区域，不混入其他页名。保留原编号和时序映射，不能为通过校验更换到一条未读或不支持该事实的来源。

## 候选与根因

error_candidates逐项记录id、step、actor、stage、偏离、后果、是否已恢复及证据。stage是分析位置，不是基础模板新标签；区分grounding、command_generation、adaptation等，给命令行/定位责任分析留下明确位置。一次被运行器拒绝、界面无状态变化、下一步立即恢复且后续不受影响的瞬时动作错误不进入error_candidates；除非重复发生或造成持续状态、决策、预算或验收影响，否则只在limitations记录。

attribution.initial/recovery为null或归因对象：cause_id引用候选；step、stage和responsible_actor与该候选一致；labels填机器ID，不填中文展示名。

hierarchy模板：labels.l1/l2/l3按真实父子路径；module/function/mechanism/object全部null。dimensions模板：l1/l2/l3全部null，独立填写module/function/mechanism/object，Initial用F1-F9/E1/E2，Recovery用R1-R5。

mapping_status=exact 需所有所需标签合法；gap 时可保留已确定的合法维度，其余 null，并在 template_gaps 说明。外部 App 或设备来源是否使用空 Module、专用外部 Module 或环境维度，只按当前 taxonomy 与 actor-mapping 的声明处理；不得为了让外部问题落入模型组件而强行映射。无适当标签不发明已有选项、不把最接近的名称伪装为完全匹配。

mapping gap只表示有证据的原因缺少合适模板标签，不表示原因已知。缺少责任来源或因果证据时保留Initial=null、absence_reason与limitations；不能用mapping_status=gap把猜测写成已确认根因。

primary_selection 解释为何选这个主要原因；secondary_causes 引用其他有证据候选。初始原因确实无法确定时允许 null，但 absence_reason 和 limitations 须明确缺口。纯预置阻塞按当前 taxonomy 的预置路径精确填写；其 cause_id 引用阻塞候选，actor=precondition、stage=environment，并链接同类型同步骤的 confirmed preconditions。taxonomy 明确允许的叶子或外部模块空值不报映射缺口。

Recovery 不能使用当前 taxonomy 声明为仅 Initial 的预置类别；外部阻塞只记 Initial 或 preconditions。Recovery 需独立候选及 recovery_independence，说明错误后新的机会和错误判断/措施；不能复用 Initial cause_id。同一步内有可区分的后续观察/决策可保留相同步骤，但必须说明阶段差异，不机械要求 Step+1。

recovery_opportunity：exists/attempted/outcome单列。yes必须有步骤和证据；no需要流程/终止证据，step可null；unknown保留未知，不写成no。outcome=succeeded时Recovery为null。已识别错误但没尝试和根本没识别，解释不同。

success：Initial和Recovery均null、absence_reason写无需失败归因，Recovery Opportunity为not_applicable。仍可在limitations写过程效率观察，不借此把成功改失败。

## 一致性约束

responsible_actor 与候选 actor 一致还不够，还必须与最终模块标签符合 actor-mapping.json 中的映射。同一别名是否可借用另一组件标签，只以当前 actor-mapping 为准；不得因名称相近跨组件借用。App 与预置问题使用外部来源；设备适配来源若当前 taxonomy 明确没有对应模型模块，可保持模块空值并报告映射缺口，不强归模型组件。

completion=success表示已有证据显示目标达成；decision=success表示完整检查后确认任务完成。最终success需要全量检查、非空验收目标全部满足、直接完成证据及空失败根因。未完成规定检查或仍有真实材料缺口时不能记 success；保留已确认的 completion 事实，并将 decision 记为 fail。

decision 只有二值，只表示任务是否完成；污染、预置条件和证据缺口分别记录，不产生额外处置值。partial、not_completed、unknown 均为 fail。fail 允许没有 unsatisfied goal：证据不足时可将 goals 保留 unknown、completion 记 unknown，并写明缺口和 absence_reason；不得用一个原因标签假装已证明具体失败。若 goal 已明确 unsatisfied，则按证据正常归因。

contamination 与 preconditions 只记录事实、影响和必要的恢复前提，不改变 decision。即使存在已确认污染、登录限制、账号权限或运行器通信限制，只要任务完成且满足 success 条件仍记 success；否则记 fail。

Recovery三字段合法组合：

| exists | attempted | outcome |
| --- | --- | --- |
| yes | yes | succeeded / failed / unknown |
| yes | no | not_attempted |
| yes | unknown | unknown |
| no | not_applicable | not_applicable |
| not_applicable | not_applicable | not_applicable |
| unknown | unknown | unknown |

有恢复机会不等于一定尝试。没有机会、不适用、未知必须区分；恢复成功不填恢复失败根因。校验组合不等于证明模型判断的语义正确。

实际访问记录的交叉核验见runtime-evidence.md；未提供回执时，校验器不验证真实阅读行为。

明确为satisfied或unsatisfied的每个验收项，都须关联支持该项判断的截图或执行回执；仅有模块自述时保留unknown。目标动作完成但有违规额外操作时，将额外操作约束单独列为unsatisfied，其余已满足目标保持satisfied；completion为partial，decision为fail，不虚构主要动作未执行。

Recovery不能以mapping_status=gap或全空标签绕过外部来源限制；账号、App或其他环境条件本身不是独立恢复缺陷。使用运行回执时，所有引用证据的来源也必须有对应访问事件，不能只核对覆盖清单。

primary_selection应解释主要偏离如何影响原始任务，必要时区分上游错误与下游阻塞；不能把未证实的应用身份、对象来源或预计剩余步数当作已知事实。

未完成目标允许由多个evidence_ids共同组成动作链证明，不要求每条引用单独充分。独立复核联合相关历史、末步实际动作/后图与明确终止条件判断；额外运行边界确认单独存档，绑定attempt和原结果哈希，不为加入后验确认而改写模型原文。

