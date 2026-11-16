---
name: attribution-bootstrap
description: "从一批 GUI 归因 cases 目录和对应框架代码仓启动可复现的归因批跑：先识别数据布局并生成 manifest/architecture，再预检和批跑；不读取人工 GT，不为未知格式猜填材料"
---

# GUI 归因批跑引导

当用户提供 cases 路径和框架代码仓路径时，使用这个操作者侧流程准备批跑。这个 Skill 不进入单 case 沙箱，也不替代单 case 的 `$gui-trajectory-attribution` 分析 Skill。

## 输入边界

- 必需：`cases` 路径、框架代码仓路径、新的输出目录。
- 真实批跑默认必需：`config/stepwise.env` 中的 Responses 网关和密钥；bootstrap 默认读取该文件，也可用 `--env-file` 覆盖。若设置 `ATTRIBUTION_MODEL_SOURCE=codex`，则改为复用本机 Codex `config.toml` 的 model/provider 与 `env_key`，不再要求 `GROUND_URL` 和 `GROUND_API_KEY`；该模式的 provider 必须是 `responses`。
- 可选：framework、pack、architecture 模式、人工 overview、case 子集、jobs、wall。
- 不读取、不复制、不展示人工 GT。只允许 benchmark adapter 读取其注册的 annotations 和 step 材料来生成模型输入。
- cases 路径必须属于 `config/datasets/failure_attribution_benchmark.json` 注册的布局。未知布局或缺少 instruction、step、图片范围等元数据时，停止并报告缺口，不猜造 manifest。
- 代码仓用于生成固定 architecture artifact。architecture 不是 attempt 证据，不能据此直接选择错误步骤或标签。
- 真实 `run` 的外层启动器必须在可绑定 `127.0.0.1` 且可出站 HTTPS 的宿主会话中执行。`run_case.py` 会启动本地 Responses 记录器；外层 Codex 沙箱若禁止网络或本地端口，预检可能通过但正式批跑会失败。
- 只有外层启动器在沙箱外；它派发的每个 case 仍必须进入各自的独立沙箱。不要全局关闭 Codex 沙箱，也不要让 case 裸跑。
- 不要在已受网络或端口限制的 Codex 会话中再启动一个 Codex 来规避限制；子进程会继承外层沙箱。不要用 Computer Use 绕过终端限制，也不要改成丢失送达回执的直连模式。应生成启动脚本交给用户在其普通终端执行，或由宿主显式授权沙箱外运行该启动器。

## 工作流

### 1. 先检查，不写文件

```bash
python3 data/bootstrap_attribution.py inspect \
  --cases /path/to/cases \
  --code /path/to/framework-repo
```

确认输出中的 `layout.framework`、`layout.cases_root`、`case_directories` 和 `code.repo_root`。多框架数据集根目录会按代码仓的 pack source root 反推 framework；仍不唯一或不受支持时，要求用户补充 framework 或数据格式说明。`case_directories` 不是最终 adapter 选中的 case 数量，后者以 `prepare` 输出为准。

### 2. 生成中间文件和预检

```bash
python3 data/bootstrap_attribution.py prepare \
  --cases /path/to/cases \
  --code /path/to/framework-repo \
  --out runs/bootstrap_YYYYMMDD \
  --env-file config/stepwise.env \
  --preflight
```

`prepare` 会生成：

- `inputs/prediction_manifest.json`：数据适配器生成的原始模型输入清单
- `inputs/run_manifest.json`：绑定 architecture 后的批跑清单
- `architecture/<framework>/`：固定源码 revision 和哈希的 architecture artifact
- `bootstrap.json`：输入、case 范围、manifest 哈希、architecture 绑定和预检记录

预检失败时不要绕过；先修正 CLI、运行环境、材料路径或代码仓状态。

默认模型源下，若用户尚未配置 `.env`，先要求从 `config/stepwise.env.example` 复制并填写 `GROUND_URL`、`GROUND_API_KEY`。用户也可以显式选择 `ATTRIBUTION_MODEL_SOURCE=codex` 复用其 Codex 配置。不要提示密钥或把它写入结果。

### 3. 启动批跑

```bash
python3 data/bootstrap_attribution.py run \
  --prepared runs/bootstrap_YYYYMMDD \
  --env-file config/stepwise.env \
  --jobs 8 \
  --wall 3600
```

这条命令的外层进程必须在沙箱外，case 子进程仍在沙箱内。若当前会话不能绑定本地端口，不要假装已经启动；把准备好的启动命令交给用户在其普通终端执行，并明确说明失败原因是外层启动器受沙箱限制。预检通过不能替代真实启动检查。

如只跑部分 case，追加 `--case-id case_001 --case-id case_002`。恢复未派发 case 时使用同一个 batch 输出目录并追加 `--resume`；不要覆盖已有 attempt。

## 完成后检查

1. 先看 `bootstrap.json` 的 manifest 哈希和 case 数量。
2. 看批跑目录的 `batch_state.json` 和每个 case 的 `status.json`。
3. 区分 `validated`、`invalid`、`runtime_failed`、`pending`。`validated` 只表示程序检查通过，不表示与人工 GT 一致。
4. 需要查看结果时，读取每个 case 的 `result.json`、`normalization_audit.json` 和组装审计；不要从日志文本中重新拼造结果。

## 禁止事项

- 不直接修改 cases、annotations、GT 或已有 attempt。
- 不把 architecture 源码内容当作当前轨迹证据。
- 不在布局不明确时静默使用某个 adapter。
- 不在 `prepare` 失败后直接调用 `run_batch.py` 跳过绑定和预检。
