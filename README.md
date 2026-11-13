# GUI轨迹归因：Codex CLI逐Step输出

使用Codex CLI原生加载归因Skill，完整读取GUI任务的日志和网站图库，按Step保存局部判断，再提交全局归因，由程序组装并校验最终结果。支持Windows、macOS和Linux；Windows使用PowerShell提交helper，macOS/Linux使用Python提交helper。

版本：**Codex CLI 0.166.0**。

| 框架 | 归因模板 |
|---|---|
| Mobile-Agent-v3 | L1／L2／L3分层标签与模块职责 |
| Agent-S2 | L1／L2／L3分层标签与框架职责映射 |
| UniPath | Module／Function／Mechanism／Object |

三框架共用输出协议，分别使用各自的职责映射和标签。运行器接收整理后的材料清单；接入其他框架时需核对当前attempt、日志步骤映射和网站图库范围。

Skill负责分析方法，启动Prompt提供任务与资源入口，提交协议定义工具及文件接口。见 [提交流程](docs/PROTOCOL.md)。

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
  protocols/submission_protocol_posix.md  macOS/Linux分步提交约定
  engine/
    native_engine.py           原生CLI、材料准备与送达记录
    responses_transport.py     Responses请求与响应流原样转发
    stepwise_support_v2.py     分步结构、全局结构、程序组装
    stepwise_support_fast.py   实际启动Prompt和交互提速安排
    io.ps1                     Windows步骤提交与基础检查
    io.py                      macOS/Linux步骤提交与基础检查
    platform_runtime.py        Windows/macOS/Linux沙箱与进程策略
    validate_result.py         这轮实际使用的最终校验器
    normalization_audit.py     规范化和组装过程的字段变化记录
    harness/                   输出协议、证据索引和本地存储
    study/                     材料读取与网站图库范围
  skills/v3/
    manifest.json
    gui-trajectory-attribution/
      SKILL.md
      references/
      agents/
      scripts/validate_result.py
docs/
  examples/                    当前Prompt示例及分步／全局Schema
  source-manifest.json          原始文件哈希和导出调整
  PROTOCOL.md                  提交流程与校验约定
tests/                         离线检查，不向归因模型加载
RELEASE_MANIFEST.json           导出运行文件的SHA256
```

运行入口为 `stepwise/run_case.py` 和 `stepwise/run_batch.py`。

## 安装与配置

要求Python 3.10+、Node、Codex CLI **0.166.0**。入口检查CLI版本。Windows使用MXC沙箱和PowerShell运行时；macOS使用Seatbelt；Linux使用bubblewrap（需要预先安装）。

```powershell
npm install -g @openai/codex@0.166.0
python -m pip install -r requirements.txt
Copy-Item config/stepwise.env.example config/stepwise.env
```

macOS/Linux把最后一行换成 `cp config/stepwise.env.example config/stepwise.env`，并把 `python` 换成 `python3`。

在本地 `config/stepwise.env` 填写公司HTTPS Responses接口地址和密钥，该文件已被Git忽略。可填网关基础地址 `https://<gateway>/v1` 或完整 `/v1/responses`。网关必须支持当前模型的原生Responses流和工具调用。可选设置 `CODEX_BINARY`（JSON参数数组）。Windows可设置 `CODEX_RUNTIME_DEP` 指向Codex PowerShell运行时目录；macOS/Linux不需要该运行时。

## 输入材料

输入为 `prediction_manifest.json`。清单只含任务身份、证据目录和图片范围，不含GT。顶层有 `cases`、`image_delivery`；每条包含 `case_id`、`identity`、`evidence_root`、`inventory`、`available_steps`、`images`、`image_count`、`image_scope`。

`inventory`保存attempt内的相对路径及源文件SHA256；`images`保存实际发送图片的文件路径和SHA256；`image_scope`声明与人工网站一致的图片内容、顺序和数量。代码仓库不携带原始轨迹、图片缓存和GT。

材料根目录与缓存支持绝对路径，也支持相对清单文件的路径。换电脑时按新目录调整路径，保留文件哈希及原任务、attempt身份。

图片使用**清单登记的q85 WebP缓存，保持原图尺寸**，属于有损编码，不是原始PNG字节或无损压缩。执行器直接读取清单登记的图片缓存。

UniPath 的打包标注证据可用独立适配器生成清单。适配器只读取 `trajectory_digest.md`、`contact_sheet_01.png` 和可选的 `tail_sheet.png`，不会读取 `02_ATTRIBUTION_RESULTS` 中的人工GT。contact sheet 的固定 5×6 网格会无损切成逐步 cell 图，模型只收到逐步图、digest 和最终观察图，不需要在运行时反复放大整张 contact sheet；切图来源哈希和裁剪框记录在清单中：

```bash
python3 data/build_uipath_manifest.py \
  --base /path/to/qwen_uipath_teacher_share_20261105_final \
  --out data/uipath_qwen_manifest.json
```

可用 `--cases` 只登记指定 case ID。该适配器与 Mobile-Agent 的 `data/build_prediction_manifest.py` 相互独立。

macOS/Linux另外支持受控派生视图：模型可先全图送达，再用`io.py derive`对已登记原图做确定性裁剪和1—4倍缩放。派生视图只用于局部放大核对，必须用父图`Sxxxx`作为证据来源，不进入图库清单、图片数量或覆盖统计，并单独记录父图哈希、裁剪框、缩放比例和派生字节哈希。其他工具生成的未登记图片会被桥接层拒绝，不会中断案例；原图仍是唯一证据源。Windows的PowerShell提交和MXC运行契约保持不变。

## 启动

先预检：只核对代码、Skill、材料哈希、CLI和运行时，不请求模型。

```powershell
python stepwise/run_case.py --manifest data/prediction_manifest.json --case case_001 --out runs/check_case001 --preflight
```

单案例真实运行：

```powershell
python stepwise/run_case.py --manifest data/prediction_manifest.json --case case_001 --out runs/case001_a1 --env-file config/stepwise.env --wall 3600
```

macOS/Linux真实运行示例：

```bash
python3 stepwise/run_case.py \
  --manifest data/prediction_manifest.json \
  --case case_001 \
  --out runs/case001_a1 \
  --env-file config/stepwise.env \
  --wall 3600
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

使用派生视图时，`derived_view_audit.json`单独记录送达的派生图及其父图绑定；`access_receipt.json`仍只反映原图与文本来源。

1. Windows的`io.ps1`或macOS/Linux的`io.py`检查基础字段、步序、来源编号及补充证据一致性，记录会话内局部纠错
2. 程序组装步骤与全局文件，绑定身份、证据编号、过程和覆盖情况
3. `stepwise/engine/validate_result.py`执行最终JSON结构、引用、标签和规则一致性检查

Skill内的校验脚本保留冻结快照，与上述实际校验器版本不同。本协议要求模型不自行执行最终校验；由调用方在完成后严格校验一次。

单独执行实际校验器时，默认读取本包冻结规则，也可显式指定：

```powershell
python stepwise/engine/validate_result.py runs/case001_a1/result.json --references stepwise/skills/v3/gui-trajectory-attribution/references --evidence-root data/evidence/case_001 --access-receipt runs/case001_a1/access_receipt.json
```

最终检查前的确定性规范化，包括删除不存在的证据引用、清理定位文字、统一责任模块大小写、补未使用维度的null，以及在成功处置下统一恢复机会状态；输出协议还会剔除未声明字段。这些处理可能改变字段内容。`first_prediction.json`保留原始预测，`normalization_audit.json`和状态中的`normalization_changed`记录实际字段变化。

启动前，执行器将对应平台的submission_protocol复制到单案例工作区，与索引、Schema和提交helper一同提供：Windows使用`submission_protocol.md`和`io.ps1`，macOS/Linux使用`submission_protocol_posix.md`内容并通过`io.py`提交。它只规定本轮接口，不另定义根因、责任模块或标签判定规则。

`validated`表示程序检查通过，归因准确率另与人工GT比较。一次启动内也可能发生局部纠错，不能将它写成零错误通过。

## 离线检查

```powershell
python -m unittest discover -s tests -v
```

测试不请求付费API，不操作手机，不把测试样例送给归因模型。
