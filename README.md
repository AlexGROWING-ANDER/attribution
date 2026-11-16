# Flux 原生 GUI 轨迹归因

在 Flux 当前会话中执行 GUI 轨迹归因，按原 Skill 建立 checkpoint、读取日志及逐张截图、保存 Step 判断并提交全局归因。Python 准备材料、管理状态、确认工具送达、组装结果和执行最终校验，不启动模型进程。

入口 Skill 是 [SKILL.md](SKILL.md)，完整执行流程见 [批跑引导](stepwise/skills/operator/attribution-bootstrap/SKILL.md)。不需要 Codex CLI、model/provider、模型网关配置或额外登录态。原生 Flux 子任务继承当前会话能力，不传模型覆盖字段。

## 开始执行

环境需要 Python 3.10+ 与 requirements.txt 中的本地依赖。让 Flux 读取入口 Skill，然后提供 cases、框架代码及新输出目录。源仓库和 URL 按当前用户权限读取；无权时停止相应读取，不换身份。

```bash
python3 data/bootstrap_attribution.py inspect --cases /path/to/cases --code /path/to/codes
python3 data/bootstrap_attribution.py prepare --cases /path/to/cases --code /path/to/codes --out /path/to/prepared --preflight
python3 data/bootstrap_attribution.py run --prepared /path/to/prepared --jobs 2 --wall 3600
```

这些命令生成固定清单、架构绑定和 Flux 任务文件。随后 Flux 执行 dispatch.json 中的原生任务；命令行脚本不会通过虚构接口自动调用 Flux。原生子任务不可用时，当前 Flux 会话可顺序执行。

`--jobs` 为原生任务并发上限，范围 1—5；执行时还需服从实际 cgroup 配额。Python 准备阶段顺序执行。恢复同一范围使用 `--resume`，不覆盖已有尝试；`PAUSE` 阻止新派发。ready_for_flux 只代表材料可执行。

## 输入与归因规则

支持 `Agent_S2`、`Mobile_Agent_v3`、`uipath`。目录和数据包名称不区分大小写，保留实际路径，兼容 `ui_path` 等旧名称；多个匹配目录时报歧义。

每条案例清单绑定原任务、attempt、source ID、材料哈希、步骤映射和人工网站图库范围，不包含 GT。原图使用原有 q85 WebP 缓存、保持尺寸；有损交付选择保持明确记录，不重新压缩或生成 contact sheet。

架构概览由固定源码或人工概览生成，只用于理解框架，不作为 attempt 证据。真实 Git 源码保留 HEAD 和脏文件检查，未跟踪材料副本不因父目录 Git 仓误判。

冻结的归因 Skill、Prompt、标签、checkpoint 和逐步提交协议、输出 Schema、结果组装和最终校验算法保持原版本。单案例工作区只包含其材料与必要工具，不复制 GT、评分及其他案例。

## 工具读取及送达记录

每条任务的 flux_task.md 给出本地 read/acknowledge 工具门面：文本完整展示后确认；图片须由原生 view_image 实际展示后确认。仅列路径或生成工作区不算送达；截断和失败不确认。原 io.py derive 生成的受控派生视图经同一门面登记，不能替代原图库覆盖。

access_receipt.json 的 producer 保持原校验器要求的 harness，confirmation_producer 为 flux_agent。这是 agent 对原生工具展示成功的确认，明确标识为非上游 HTTP 传输验证，不声称独立验证平台 trace 或模型理解。原校验器继续核对完整访问事件、源哈希、引用与覆盖。

## 提交与完成

Flux 子任务先提交 checkpoint，再按顺序经 io.py 保存 Step 和 global_summary.json，执行本地 check，回复 DONE。父任务执行：

```bash
python3 stepwise/run_case.py --manifest /path/to/prepared/inputs/run_manifest.json --case CASE_ID --out /path/to/prepared/batch/runs/CASE_ID/attempt_01 --finalize
python3 stepwise/run_batch.py --manifest /path/to/prepared/inputs/run_manifest.json --out /path/to/prepared/batch --collect --resume
```

父任务按原算法组装并严格校验一次，不进行最终校验反馈后的模型回修。只有 validated 算程序校验通过；它不等于归因准确率。尚未提交、invalid 和运行阻塞分别统计，不能以材料准备或 DONE 单句替代完整结果。

每条尝试保留 input_binding.json、flux_session.json、Prompt、工具确认票据、io_events、回执、checkpoint、Step、global_summary、result、status 及归一化审计。原生子任务的标识和完成结果由 Flux 父任务保留。

## 代码与检查

`stepwise/run_case.py` 是准备/最终校验入口；`stepwise/run_batch.py` 管理批次和原生任务清单；`stepwise/engine/flux_execution.py` 管理冻结工作区和工具确认。native_engine.py 保留旧执行器及共享算法以便审计，当前入口只复用其材料、组装及校验函数，不调用旧模型执行器。旧 transport 和沙箱代码不参与新执行流程。

```bash
python3 -m unittest discover -s tests -q
```

离线检查不发模型请求；真实原生执行单独保留记录。离线历史 GUI 材料归因不冒充业务 API/GUI 测试。
