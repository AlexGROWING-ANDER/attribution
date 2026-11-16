# GUI轨迹归因：Codex CLI逐Step输出

使用Codex CLI原生加载归因Skill，先依据任务建立并提交任务级checkpoint计划，再完整读取GUI任务的日志和网站图库、按Step保存局部判断，最后提交全局归因，由程序组装并校验最终结果。checkpoint只固定验收覆盖，不是证据，也不直接决定归因标签。支持Windows、macOS和Linux；Windows使用PowerShell提交helper，macOS/Linux使用Python提交helper。

基准版本：**Codex CLI 0.166.0**。入口默认只记录实际版本，不启用硬性门禁；设置 `ATTRIBUTION_ENFORCE_CODEX_VERSION=1` 后才会强制精确匹配，匹配版本可用 `ATTRIBUTION_CODEX_VERSION` 覆盖。

框架实现差异由外部 `framework pack` 和可校验的 architecture artifact 提供，不写死在归因代码或 Skill 中。architecture 可以由固定 revision 的源码仓静态生成，也可以由人工概览生成；两种模式使用同一套 artifact、安装和校验契约。标签体系、L1／L2／L3 与责任维度仍由评测配置定义，不由源码自动推断。运行器接收整理后的材料清单；接入其他框架时需核对当前 attempt、日志步骤映射和网站图库范围。

Skill负责分析方法，启动Prompt提供任务与资源入口，提交协议定义工具及文件接口。见 [提交流程](docs/PROTOCOL.md)。

## 实际调用方式

```text
有界批跑器（默认8路，最多16路）
  → 每条案例独立启动 Codex CLI（基准 0.166.0）
  → 原生加载Skill，使用原生exec_command／view_image读取材料
  → 本地送达记录器：原生Responses请求／响应透传
  → 公司网关 /responses：deepseek-v4.1-flash
```

本地记录器不做Responses与Chat Completions转换，不改工具定义、工具返回或响应事件格式。它恢复约定图片字节，记录送达回执，并将网关的Responses流按原字节转回Codex。审批请求仍由Codex发起，记录器仅将审批模型名明确路由到同一DeepSeek模型，这是项目的审批代理配置，不声称使用OpenAI官方审批模型。网关内部如何实现Responses不属于本仓可观测范围。

模型工作区只包含当前材料、Skill和提交工具。GT、评分文件和历史预测不进入工作区。送达回执能说明材料进入请求，不能证明模型已经注意并正确理解全部材料。

## 从两个路径开始

操作者或Code Agent拿到一批cases路径和对应框架代码仓路径后，先用bootstrap做布局识别、manifest生成、architecture绑定和可选预检，再启动批跑。完整说明见[Skill使用说明](docs/SKILL_USAGE.md)，操作者侧流程见[attribution-bootstrap](stepwise/skills/operator/attribution-bootstrap/SKILL.md)。

真实运行默认使用 `stepwise` 模型源：从`config/stepwise.env.example`复制为`config/stepwise.env`，填写`GROUND_URL`和`GROUND_API_KEY`；bootstrap未传`--env-file`时默认使用该文件。`inspect`不读取它，`prepare`不强制读取它，真实`run`需要它。

也可以设置 `ATTRIBUTION_MODEL_SOURCE=codex`，从本机 Codex 的 `config.toml` 读取当前 `model`、`model_provider` 及其 `base_url`／`env_key`，不再要求 `GROUND_URL` 和 `GROUND_API_KEY`。该模式仍经过本地记录器，因此保留送达回执和请求审计；`ATTRIBUTION_CODEX_HOME` 可覆盖 Codex 配置目录，`ATTRIBUTION_CODEX_MODEL` 可覆盖模型名。不设置该变量时默认仍为 `stepwise`。

```bash
python3 data/bootstrap_attribution.py inspect --cases /path/to/cases --code /path/to/framework-repo

python3 data/bootstrap_attribution.py prepare \
  --cases /path/to/cases \
  --code /path/to/framework-repo \
  --out runs/bootstrap_YYYYMMDD \
  --env-file config/stepwise.env \
  --preflight

python3 data/bootstrap_attribution.py run \
  --prepared runs/bootstrap_YYYYMMDD \
  --env-file config/stepwise.env \
  --jobs 8 \
  --wall 3600
```

运行边界：`inspect`和`prepare`可以在受限沙箱中执行，但真实`run`的启动器必须在能够绑定`127.0.0.1`并可出站访问HTTPS的宿主会话中执行。需要放到沙箱外的是`run_batch.py`这个外层启动器，不是所有Codex命令；它启动的每个case仍进入各自独立的沙箱。不要在已经受网络或端口限制的Codex会话里再启动一个Codex来规避限制，子进程会继承外层沙箱；应改用普通终端或在宿主机上显式授权该启动器。预检不请求模型，预检通过不代表真实批跑能在同一沙箱中完成。

当前注册的数据布局在`config/datasets/failure_attribution_benchmark.json`。不支持的结构会停止并报告缺口，不会猜造manifest。人工GT不进入bootstrap、清单或模型工作区。

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
    framework_architecture.py  源码架构编译、绑定解析与沙箱安装
    responses_transport.py     Responses请求与响应流原样转发
    stepwise_support_v2.py     checkpoint/分步/全局结构、程序组装
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
config/frameworks/<framework>/pack.json  框架源码范围与architecture入口
data/build_framework_architecture.py      编译architecture并写入清单绑定
data/bootstrap_attribution.py             两路径输入、布局识别与准备/批跑编排
config/datasets/                         数据集adapter注册表
docs/
  examples/                    当前Prompt示例及分步／全局Schema
  source-manifest.json          原始文件哈希和导出调整
  legacy/framework-profiles-manual.md  早期人工框架职责归档（不参与运行时）
  PROTOCOL.md                  提交流程与校验约定
  SKILL_USAGE.md               操作者与模型侧Skill使用说明
stepwise/skills/operator/attribution-bootstrap/  操作者侧批跑引导Skill
tests/                         离线检查，不向归因模型加载
RELEASE_MANIFEST.json           导出运行文件的SHA256
```

运行入口为 `stepwise/run_case.py` 和 `stepwise/run_batch.py`。

## 安装与配置

要求Python 3.10+、Node、Codex CLI（基准 **0.166.0**）。入口读取并记录实际CLI版本；默认不因版本不同而阻断。需要严格复现时设置 `ATTRIBUTION_ENFORCE_CODEX_VERSION=1`，并用 `ATTRIBUTION_CODEX_VERSION` 指定要求版本。Windows使用MXC沙箱和PowerShell运行时；macOS使用Seatbelt；Linux使用bubblewrap（需要预先安装）。

```powershell
npm install -g @openai/codex@0.166.0
python -m pip install -r requirements.txt
Copy-Item config/stepwise.env.example config/stepwise.env
```

macOS/Linux把最后一行换成 `cp config/stepwise.env.example config/stepwise.env`，并把 `python` 换成 `python3`。

在本地 `config/stepwise.env` 填写公司HTTPS Responses接口地址和密钥，该文件已被Git忽略。可填网关基础地址 `https://<gateway>/v1` 或完整 `/v1/responses`。网关必须支持当前模型的原生Responses流和工具调用。若希望直接复用本机 Codex 当前配置，设置 `ATTRIBUTION_MODEL_SOURCE=codex`；它要求所选 provider 使用 `wire_api = "responses"`，并以 `env_key` 指定的环境变量提供密钥。可选设置 `CODEX_BINARY`（JSON参数数组）。Windows可设置 `CODEX_RUNTIME_DEP` 指向Codex PowerShell运行时目录；macOS/Linux不需要该运行时。

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

## 框架架构输入

`config/frameworks/<framework>/pack.json` 只描述架构材料的来源和编译范围，例如仓库环境变量、源码根目录、include／exclude，以及默认使用 `generated` 还是 `manual` 模式。它不包含归因标签、责任模块或当前 attempt 的结论。

从源码仓生成 architecture，并把固定绑定写入输入清单：

```bash
MOBILE_AGENT_V3_REPO=/path/to/Mobile-Agent-v3 \
python3 data/build_framework_architecture.py \
  --framework mobile_agent_v3 \
  --out data/framework_architectures/mobile_agent_v3_v1 \
  --manifest data/input_manifest.json \
  --manifest-out data/input_manifest_with_architecture.json
```

使用人工概览时只需切换模式；产物契约与校验方式不变：

```bash
python3 data/build_framework_architecture.py \
  --framework mobile_agent_v3 \
  --mode manual \
  --manual-overview /path/to/manual-overview.md \
  --out data/framework_architectures/mobile_agent_v3_manual_v1 \
  --manifest data/input_manifest.json \
  --manifest-out data/input_manifest_with_architecture.json
```

编译产物目录包含 `artifact.json`、`architecture.json` 和 `architecture.md`。生成内容包含模块摘要与导入关系、类字段、`self.*` 实例状态及方法内读写位置、组件接口、动作常量和调用／继承关系；实例字段的 `value` 只记录 `__init__` 中可解析的默认字面量。编译会记录源码 revision、选中源码树摘要、artifact／JSON／Markdown 哈希；Git 仓会拒绝选中文件的未提交修改，非 Git 仓使用内容固定 revision。清单用顶层 `framework_architecture` 绑定单框架，或用 `framework_architectures` 登记多框架；运行前会重新核验路径、哈希和 case identity，再安装到案例工作区。

architecture 只用于理解框架组件、共享状态、接口、动作契约和调用关系。它不是 attempt 证据，不能替代日志、截图或送达回执，也不能用来直接判定根因。

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

启动前，执行器将对应平台的submission_protocol复制到单案例工作区，与索引、Schema和提交helper一同提供：Windows使用`submission_protocol.md`和`io.ps1`，macOS/Linux使用`submission_protocol_posix.md`内容并通过`io.py`提交。`checkpoint_plan.json`必须先经helper提交并通过哈希绑定，之后才允许派生视图、Step、全局汇总和检查；每个required checkpoint必须在最终`goals`中保持相同`id`、`object`和`requirement`。该门禁约束helper提交与派生视图，不声称能阻止直接读取原始文件。它只规定本轮接口，不另定义根因、责任模块或标签判定规则。

`validated`表示程序检查通过，归因准确率另与人工GT比较。一次启动内也可能发生局部纠错，不能将它写成零错误通过。

## 离线检查

```powershell
python -m unittest discover -s tests -v
```

测试不请求付费API，不操作手机，不把测试样例送给归因模型。
