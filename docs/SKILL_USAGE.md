# Skill 使用说明

本仓有两层 Skill，职责分开：

- **单 case 归因 Skill**：`stepwise/skills/v3/gui-trajectory-attribution/SKILL.md`，由 runner 放进每个 case 的独立沙箱，模型用它分析日志、截图和 checkpoint。
- **操作者引导 Skill**：`stepwise/skills/operator/attribution-bootstrap/SKILL.md`，供 Code Agent 或人工操作者从一批 cases 和框架代码仓准备批跑，不进入单 case 沙箱。

## 1. 准备环境

- Python 3.10+
- Codex CLI 0.166.0
- `config/stepwise.env` 中的 Responses 网关和密钥
- macOS、Linux 或 Windows 对应的运行时依赖

先创建运行配置：

```bash
cp config/stepwise.env.example config/stepwise.env
```

必填：

- `GROUND_URL`：HTTPS Responses 网关地址，或完整的 `/v1/responses` 地址。
- `GROUND_API_KEY`：对应网关密钥。

按需填写 `CODEX_BINARY` 和 `CODEX_RUNTIME_DEP`。`config/stepwise.env` 已被 Git 忽略，不要提交真实密钥。`inspect` 不需要该文件；`prepare` 不强制读取；`prepare --preflight` 和真实 `run` 需要它。bootstrap 在未传 `--env-file` 时默认使用 `config/stepwise.env`。

如果还没配置其他环境，先按 `README.md` 的“安装与配置”完成。

## 2. 运行边界：启动器在沙箱外，case 在沙箱内

`run_case.py` 会在宿主进程内启动一个只绑定 `127.0.0.1` 的本地 Responses 记录器，用它接收 Codex CLI 的请求、原样转发到 `GROUND_URL`，并生成送达回执和请求审计。因此启动 `run_batch.py` 的进程必须同时具备：

- 绑定本机回环端口的能力；
- 访问 `GROUND_URL` 的出站 HTTPS 和 DNS 能力。

如果 Codex 会话本身运行在禁止网络和本地端口监听的沙箱中，`run_batch.py` 会继承该限制，通常表现为 `PermissionError`、无法绑定端口或 DNS 解析失败。预检不发起模型请求，所以可能仍然通过；这不代表真实批跑可以在同一沙箱中完成。

推荐的目标进程边界是：

```text
普通终端或显式授权的宿主会话
  └─ data/bootstrap_attribution.py run / stepwise/run_batch.py   ← 沙箱外，启动本地代理
       └─ 每个 case 的 Codex CLI                                 ← 各自的独立沙箱内
```

只有外层批跑启动器需要离开沙箱；单 case 的隔离不能取消。不要在已经受网络或端口限制的 Codex 会话中再启动一个 Codex 来尝试绕过限制，子进程会继承外层沙箱，仍无法绑定端口。应直接在普通 macOS/Linux 终端或 Windows 终端中运行准备好的启动命令，或由当前宿主显式授权在沙箱外执行该命令。不要全局关闭 Codex 沙箱，也不要为了启动代理而让所有 case 裸跑。

仓库当前没有“跳过本地记录器、直接连接网关”的生产模式。直连会失去 `access_receipt.json`、请求路由记录和材料送达审计，因此不能作为正式 benchmark 结果。遇到沙箱限制时，应在普通用户终端运行已经准备好的启动脚本，或由宿主显式授权在沙箱外执行 `run_batch.py`。

## 3. 输入什么

最小输入是两个路径：

1. **cases 路径**：可以是数据集根目录、某个框架的 package 目录，或实际 cases 目录。数据集根目录同时包含多个框架时，bootstrap 会用代码仓的 source root 反推 framework；仍无法唯一确定时加 `--framework`。
2. **框架代码仓路径**：可以是代码仓本身，也可以是包含代码仓的上一级目录；bootstrap 会按 pack 的 `source_roots` 定位。

当前注册的布局在 `config/datasets/failure_attribution_benchmark.json`：

- Mobile-Agent：`mobile_Agent_v3/gpt5.6/cases/converted_121cases`
- Agent-S2：`agent_s2/gpt5.6-sol/cases/agents`
- UiPath：`ui_path/gpt5.6/cases/unipath_130`

人工 GT 不作为 bootstrap 输入，也不会写入 manifest 或模型工作区。

## 4. 先 inspect

```bash
python3 data/bootstrap_attribution.py inspect \
  --cases /path/to/cases \
  --code /path/to/framework-repo
```

输出应包含识别到的 framework、cases root、目录数量、annotation 和 code repo root。`layout.case_directories` 是目录数量，不必然等于最终 manifest 的 case 数量；`prepare` 报告 adapter 实际选中的 scope。若布局不支持或出现歧义，先补充 `--framework`、`--pack` 或新的 dataset adapter，不要猜造材料。

## 5. 再 prepare

```bash
python3 data/bootstrap_attribution.py prepare \
  --cases /path/to/cases \
  --code /path/to/framework-repo \
  --out runs/bootstrap_YYYYMMDD \
  --env-file config/stepwise.env \
  --preflight
```

`--env-file` 可以省略，使用默认的 `config/stepwise.env`。

输出目录是新建的，不能复用已有批跑目录。生成内容：

```text
runs/bootstrap_YYYYMMDD/
  bootstrap.json
  inputs/
    prediction_manifest.json
    run_manifest.json
  architecture/
    <framework>/
      artifact.json
      architecture.json
      architecture.md
  preflight/
    <first-case>/
```

`inputs/run_manifest.json` 是后续批跑的唯一入口清单；它带有 architecture 绑定和固定哈希。

常用选项：

- `--case-id case_001`：只准备指定 case，可重复。
- `--mode manual --manual-overview /path/to/overview.md`：使用人工架构概览，不编译源码。
- `--revision <sha>`：要求源码仓 revision 与预期一致。
- `--source-root <path>`：覆盖 pack 中的源码根目录，可重复。

## 6. 最后 run

```bash
python3 data/bootstrap_attribution.py run \
  --prepared runs/bootstrap_YYYYMMDD \
  --env-file config/stepwise.env \
  --jobs 8 \
  --wall 3600
```

`run` 默认沿用 `prepare` 记录的 env 文件；显式传 `--env-file` 可以覆盖。

如果只跑部分 case：

```bash
python3 data/bootstrap_attribution.py run \
  --prepared runs/bootstrap_YYYYMMDD \
  --case-id case_001 \
  --case-id case_002
```

同一个 batch 目录恢复时追加 `--resume`。已有 attempt 不会被覆盖；失败重跑应使用新的 batch 输出目录。

## 7. 结果在哪里

- `bootstrap.json`：准备阶段的输入、manifest 哈希、architecture 绑定和预检状态。
- `batch_state.json`：批跑调度状态。
- `runs/<case>/attempt_01/status.json`：单 case 状态。
- `runs/<case>/attempt_01/result.json`：最终组装结果。
- `runs/<case>/attempt_01/stepwise_assembly_audit.json`：checkpoint 和步骤组装审计。

状态含义：

- `validated`：程序检查通过；归因准确性仍需与人工 GT 比较。
- `invalid`：最终结构或规则校验未通过。
- `runtime_failed`：运行器、沙箱、CLI 或网络等基础设施失败。
- `pending` / `not_dispatched`：尚未完成或尚未派发。

## 8. 什么时候不能自动继续

以下情况必须停下来报告，而不是继续调用 runner：

- 目录结构不受支持，或同时匹配多个 adapter。
- 缺少 instruction、attempt、step 映射、日志、截图或图片范围。
- 代码仓找不到 pack 要求的 source root，或源码有未提交修改而无法固定 revision。
- `prepare` 的预检失败。
- manifest 在准备后被修改，导致 `bootstrap.json` 中的哈希不一致。

这时应补数据格式说明、adapter、pack 或运行配置，再重新 prepare 到新目录。
