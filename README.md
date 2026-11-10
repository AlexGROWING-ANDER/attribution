# GUI轨迹归因：Codex CLI逐Step输出

使用Codex CLI原生加载归因Skill，完整读取GUI任务的日志和网站图库，按Step保存局部判断，再提交全局归因，由程序组装并校验最终结果。

版本：**Codex CLI 0.166.0**。已验证的任务来自Mobile-Agent-v3 Hard Intents。Skill另含Agent-S2和UniPath模板，但这轮60条结果不能代表它们的实测表现。

| 框架 | 规则与分类模板 | 执行验证范围 |
|---|---|---|
| Mobile-Agent-v3 | 已包含 | 历史60条及当前入口两例真实联调，结果分别记录 |
| UniPath | 已包含 | 同学已独立验证；该验证的代码版本和记录未纳入本仓验收材料 |
| Agent-S2 | 已包含 | 当前发布入口尚待真实联调 |

三框架共用输出协议，分别使用各自的职责映射和标签。运行器接收整理后的材料清单；接入其他框架时需核对当前attempt、日志步骤映射和网站图库范围，并非直接读取任意原始轨迹目录即可运行。

当前启动Prompt为 `stepwise-prompt-r2`：Skill负责分析方法，启动文本提供任务与资源入口，独立提交协议定义工具及文件接口。当前Responses透传已完成两例完整图文调用：一例validated，一例因追加证据修改了原记录而在组装时invalid。此前Chat转换版两例最终校验2/2通过，各有一次局部纠错，两轮分别记录；完整60条尚未重跑。见 [Prompt说明](docs/PROMPT_CHANGES.md) 与 [联调记录](docs/SMOKE_TEST.md)。

## 实际调用方式

```text
有界批跑器（默认8路，最多16路）
  → 每条案例独立启动 Codex CLI 0.166.0
  → 原生加载Skill，使用原生exec_command／view_image读取材料
  → 本地送达记录器：原生Responses请求／响应透传
  → 公司网关 /responses：deepseek-v4.1-flash
```

本地记录器不做Responses与Chat Completions转换，不改工具定义、工具返回或响应事件格式。它恢复约定图片字节，记录送达回执，并将网关的Responses流按原字节转回Codex。审批请求仍由Codex发起，记录器仅将审批模型名明确路由到同一DeepSeek模型，这是项目的审批代理配置，不声称使用OpenAI官方审批模型。网关内部如何实现Responses不属于本仓可观测范围。

模型工作区只包含当前材料、Skill和提交工具。GT、评分文件和历史预测不进入工作区。送达回执能说明材料进入请求，不能证明模型已经注意并正确理解全部材料。

## 目录

```text
stepwise/
  run_case.py                  可移植单案例入口
  run_batch.py                 8—16路有界批跑，也支持1—7路
  prompts/stepwise_en.txt       统一启动Prompt模板
  protocols/submission_protocol.md  本轮工具、分步提交与完成约定
  engine/
    native_engine.py           原生CLI、材料准备与送达记录
    responses_transport.py     Responses请求与响应流原样转发
    stepwise_support_v2.py     分步结构、全局结构、程序组装
    stepwise_support_fast.py   实际启动Prompt和交互提速安排
    io.ps1                     步骤提交与基础检查
    validate_result.py         这轮实际使用的最终校验器
    normalization_audit.py     原有规范化和组装过程的字段变化记录
    legacy_engine/             原生路径需要的材料／输出协议依赖
  skills/v3/
    manifest.json
    gui-trajectory-attribution/
      SKILL.md
      references/
      agents/
      scripts/validate_result.py
docs/
  examples/                    历史实际Prompt及分步／全局Schema
  source-manifest.json          原始文件哈希和导出调整
  PROTOCOL.md                  提交流程与原版本关系
  RESULTS.md                   历史结果口径及限制
tests/                         离线检查，不向归因模型加载
RELEASE_MANIFEST.json           导出运行文件的SHA256
```

原仓库的 `runner/native_case_runner.py` 继续保留供历史对照，原说明移到 `docs/legacy-native-responses.md`。新入口使用 `stepwise/`。历史Chat转换模块保留用于旧版对照测试，当前原生入口不加载它们，也不在失败时退回Chat接口。

## 安装与配置

要求Windows、Python 3.10+、Node、Codex CLI **0.166.0**。入口检查CLI版本。

```powershell
npm install -g @openai/codex@0.166.0
python -m pip install -r requirements.txt
Copy-Item config/stepwise.env.example config/stepwise.env
```

在本地 `config/stepwise.env` 填写公司HTTPS Responses接口地址和密钥，该文件已被Git忽略。可填网关基础地址 `https://<gateway>/v1` 或完整 `/v1/responses`；旧配置若以 `/chat/completions` 结尾，入口会改用同一网关的 `/responses`，不会发送Chat请求。网关必须支持当前模型的原生Responses流和工具调用。可选设置 `CODEX_BINARY`（JSON参数数组）、`CODEX_RUNTIME_DEP`（Codex PowerShell运行时目录）。未指定时按常规npm安装位置发现。

## 输入材料

沿用冻结的 `prediction_manifest.json`。清单只含任务身份、证据目录和图片范围，不含GT。顶层有 `cases`、`image_delivery`；每条包含 `case_id`、`identity`、`evidence_root`、`inventory`、`available_steps`、`images`、`image_count`、`image_scope`。

`inventory`保存attempt内的相对路径及源文件SHA256；`images`保存实际发送图片的文件路径和SHA256；`image_scope`声明与人工网站一致的图片内容、顺序和数量。代码仓库不携带原始轨迹、图片缓存和GT。

材料根目录与缓存支持绝对路径，也支持相对清单文件的路径。换电脑时按新目录调整路径，保留文件哈希及原任务、attempt身份。

历史批次使用**已有q85 WebP缓存，保持原图尺寸**，属于有损编码，不是原始PNG字节或无损压缩。执行器直接读取清单登记的图片缓存。

## 启动

先预检：只核对代码、Skill、材料哈希、CLI和运行时，不请求模型。

```powershell
python stepwise/run_case.py --manifest data/prediction_manifest.json --case case_001 --out runs/check_case001 --preflight
```

单案例真实运行：

```powershell
python stepwise/run_case.py --manifest data/prediction_manifest.json --case case_001 --out runs/case001_a1 --env-file config/stepwise.env --wall 3600
```

并发运行清单中全部案例，也可通过 `--cases case_001 case_002` 指定范围：

```powershell
python stepwise/run_batch.py --manifest data/prediction_manifest.json --out runs/batch01 --env-file config/stepwise.env --jobs 8 --wall 3600
```

`--jobs`允许1—16。`--stop-on-invalid`在首个未通过结果后停止新派发，已启动任务继续完成。在批次目录创建 `PAUSE` 同样停止新派发。

移除 `PAUSE` 后，同一命令追加 `--resume` 可接着运行未派发案例。恢复要求输入清单、运行代码和Skill绑定一致，子进程启动前也核对这些哈希，避免中途换版混算。已有尝试不会被覆盖或自动重跑。失败重跑使用新批次目录，并分别保留两次记录。调度器意外退出后若留下 `batch.lock`，先核对锁中PID确认旧进程已退出，再移除锁恢复。

批跑器提供有界并发和续跑；不安装定时任务，不自动改写Skill，不启用模型最终校验反馈后的回修。

## 输出与检查

每条案例保留实际 `prompt.txt`、步骤文件、`global_summary.json`、`access_receipt.json`、组装审计、`result.json`、`status.json`及调用日志。

1. `io.ps1`做基础字段、步序和来源编号检查，记录会话内局部纠错
2. 程序组装步骤与全局文件，绑定身份、证据编号、过程和覆盖情况
3. `stepwise/engine/validate_result.py`执行最终JSON结构、引用、标签和规则一致性检查

Skill内的校验脚本保留冻结快照，与上述实际校验器版本不同。本协议要求模型不自行执行最终校验；由调用方在完成后严格校验一次。

单独执行实际校验器时，默认读取本包冻结规则，也可显式指定：

```powershell
python stepwise/engine/validate_result.py runs/case001_a1/result.json --references stepwise/skills/v3/gui-trajectory-attribution/references --evidence-root data/evidence/case_001 --access-receipt runs/case001_a1/access_receipt.json
```

原版本在最终检查前已有确定性规范化，包括删除不存在的证据引用、清理定位文字、统一责任模块大小写、补未使用维度的null，以及在成功处置下统一恢复机会状态；输出协议还会剔除未声明字段。这些处理沿用原逻辑，并非都只改变排版。本包保留 `first_prediction.json`，新增 `normalization_audit.json` 和状态中的 `normalization_changed`，记录实际字段变化；处理后validated不能称为模型原始输出零修改通过。

启动前，执行器将submission_protocol.md复制到单案例工作区，与索引、Schema和io.ps1一同提供。它只规定本轮接口，不另定义根因、责任模块或标签判定规则。

`validated`表示程序检查通过，归因准确率另与人工GT比较。一次启动内也可能发生局部纠错，不能将它写成零错误通过。

## 离线检查与历史结果

```powershell
python -m unittest discover -s tests -v
```

测试不请求付费API，不操作手机，不把测试样例送给归因模型。

逐Step60条的历史首次结果为 **47条validated、13条invalid**。更早的分模块批次经重启和修复累计60/60，两批不能混算。当前Responses透传联调两例材料均完整送达，最终validated为1/2；Chat转换版的2/2为另一轮记录。尚未完成新的60条全量验证。
