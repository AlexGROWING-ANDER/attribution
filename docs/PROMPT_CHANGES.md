# 启动Prompt与提交协议：stepwise-prompt-r2

## 分工

| 资源 | 职责 |
|---|---|
| 冻结Skill v3及其引用 | 完整图文复核、对象与状态追踪、因果链、主要根因、责任来源与标签 |
| 启动Prompt | 当前case身份、材料和Schema入口、调用哪个Skill和提交协议 |
| submission_protocol.md | 原生读图／文件工具约定、逐Step与全局提交、文件结构、纠正预算及完成标记 |
| 工程程序 | 文件保存、步序检查、来源绑定、组装、最终校验和审计 |

分析方法沿用Skill，不在启动Prompt和提交协议中再定义一套根因选择规则。提交协议随案例准备到工作区，模型必须读取，不是只把文件名写进启动文本。

## 同一case001的文本量

| 版本／资源 | 字符数 |
|---|---:|
| 历史r00_v3启动Prompt | 7555 |
| 第一轮整理r1启动Prompt | 5130 |
| 当前r2启动Prompt | 1096 |
| r2另读的提交协议 | 2982 |
| r2启动＋协议合计 | 4078 |

r2合计比原始启动文本减少约46%，比r1减少约20.5%。统计不含各版本原本必读的Skill、Schema与轨迹材料；字符数不等于token数。拆成文件本身不保证成本或耗时下降，需实际模型运行验证。

## 保持不变

Skill的17个冻结文件、任务口径、分类模板、单步Schema、全局Schema、stage_contract与assemble逻辑保持不变。独立原生读图、逐页完整读取、逐Step记录、全局提交、追加证据约束、两批定向回查和一次基础检查纠正仍由提交接口约定执行。

移除启动文本中重复的分析规则，不新增、删改标签，不改变GT，不生成归因答案。必需文件齐全且本地检查通过才回复DONE，最终严格校验由调用方执行。

## 文件入口

- 启动模板：stepwise/prompts/stepwise_en.txt
- 工程提交协议：stepwise/protocols/submission_protocol.md
- 历史实际输入：docs/examples/prompt_case001.txt
- r1留档：docs/examples/prompt_case001_r1.txt
- 当前候选示例：docs/examples/prompt_case001_optimized.txt
- 中文审核版：docs/examples/prompt_case001_optimized_中文.md

历史47/60属于旧Prompt。r2已完成两例真实联调，最终校验2/2，各有一次局部纠错，处置与GT一致1/2；尚未执行新的60条全量验证，不据此宣布模型能力或首过率提升。见SMOKE_TEST.md。
