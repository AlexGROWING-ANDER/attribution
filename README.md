# gui-attribution-native

用 **Codex CLI 原生加载技能**，对 GUI 智能体轨迹做失败归因的执行框架。

模型不是被喂一段规则文本，而是在受限工作区里**自己打开技能文件、逐页读日志、逐张看图**，
最后按调用方给的 schema 交一份归因 JSON。

---

## 1. 它解决什么

给一条 GUI 任务轨迹（日志 + 截图），判断「任务失败在哪一步、由谁造成、属于哪一层」，
并保证**模型确实看完了全部证据**——不是凭摘要猜，也不是靠模型自述。

三层判定沿用 Mobile-Agent 体系：

| 层 | 含义 | 取值示例 |
|---|---|---|
| L1 | 责任大类 | `framework` / `precondition` / `app` |
| L2 | 责任模块或阻塞类型 | `manager` / `executor` / `account_permission` … |
| L3 | 具体错误语义 | `incorrect_state_understanding` / `wrong_planning` … |

---

## 2. 一次调用（12 步）

```
 1  读 selection/READY.json → prediction_manifest.json（只有身份与证据清单，无 GT）
 2  校验技能包：逐个文件比对 sha256，不一致拒跑
 3  建沙箱 workspace，技能复制到 workspace/.agents/skills/gui-trajectory-attribution
 4  装证据：日志 → logs/page_001.txt …（每页 ≤14k 字符，带 BEGIN/END_SOURCE_RANGE 标记）
           图片 → images/*.png（内容是 q85 WebP 的字节）
 5  写 input_index.json：告诉模型有哪些日志页、哪些图
 6  拼提示词：一句 "Use $gui-trajectory-attribution …" + 调用方提交 schema
 7  起本地记录器（127.0.0.1:随机端口）
 8  启动 codex：
      codex exec --ignore-user-config --skip-git-repo-check --approve-for-me --json \
                 -C <workspace> -m <model> -o answer.txt
 9  用 -c 注入开关：
      features.skip_host_skill_discovery = true     # 只用被测的这一版技能
      permissions…filesystem = {":root":"deny", workspace:"write"}
      network.enabled = false
      request_max_retries / stream_max_retries = 0
      tool_output_token_limit = 50000
10  模型自己干活：exec_command 读日志页、view_image 读图
11  记录器把 view_image 送出的图片字节替换成约定的 q85 缓存字节，其余原样转发
12  answer.txt → 解析 → 按协议 compile → 跑技能自带校验器 → result.json / status.json
```

框架不替模型做任何决策：技能是 codex 自己发现、自己决定读哪几个 references，工具是 codex 自带的。

---

## 3. 本地记录器做什么

**不是协议网关。** 请求与响应都按 Responses 原样转发，唯一的改写点是图片字节：

| 做 | 不做 |
|---|---|
| 把 `input_image.image_url` 换成约定的 q85 字节 | 不改请求结构、不改除模型名以外的字段 |
| 记 `access_receipt`：哪张图、哪页日志进了上下文 | 不重排、不合并、不改工具定义 |
| 卡 160 次上游调用、`retries=0`、wall 截止 | 不做任何协议翻译 |

**为什么必须替换图片**：codex 会把磁盘上的图重新编码后上传（实测 188 KB 的 q85 被编码成 652 KB）。
不替换，各组看到的就不是同一份像素，A/B 对比失去前提。

**为什么必须自己记账**：`access_receipt.json` 是「模型确实看完了」的唯一硬证据，模型自述不算。

---

## 4. 目录

```
.
├── runner/
│   ├── native_case_runner.py    单案例主控
│   ├── config.example.json
│   └── skill/
│       ├── manifest.json        技能清单 + 逐文件 sha256
│       └── gui-trajectory-attribution/
│           ├── SKILL.md
│           ├── references/      分类学、证据规则、输出约定、schema
│           └── scripts/validate_result.py
└── engine/
    ├── study/                   q85 证据编码、计划与提示词
    ├── harness/                 输出协议、提交与校验
    └── supervisor/              中断恢复
```

---

## 5. 依赖

- Windows（`mxc` 沙箱、`taskkill`、`CREATE_NO_WINDOW`）
- Python 3.12+
- Codex CLI **0.162.1**
- Node（跑 `codex.js`）

```bash
pip install -r requirements.txt
```

---

## 6. 跑一条

```bash
export GROUND_URL=https://<gateway>/v1          # 需原生支持 Responses
export GROUND_API_KEY=sk-...
export CODEX_BINARY='["C:/Program Files/nodejs/node.exe","C:/Users/x/AppData/Roaming/npm/node_modules/@openai/codex/bin/codex.js"]'

python runner/native_case_runner.py \
  --case case_024 --version v3 \
  --out runs/case_024 --wall 1800
```

### 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `GROUND_URL` / `GROUND_API_KEY` | 是 | 模型网关（原生 Responses） |
| `MODEL_ENV_FILE` | 否 | 凭证文件，默认 `<repo>/config/model.env` |
| `CODEX_BINARY` | 否 | 默认自动在 npm 全局目录找 codex.js |
| `SELECTION_DIR` | 否 | 案例清单目录，默认 `<repo>/selection` |
| `SKILL_ROOT` | 否 | 技能根目录，默认 `runner/skill` |
| `CODEX_RUNTIME_DEP` | 否 | codex 运行时依赖目录（沙箱只读放行） |
| `ATTRIBUTION_MODEL` | 否 | 默认 `deepseek-v4.1-flash` |
| `MAIN_MAX_TOKENS` | 否 | 默认 24000 |
| `ATTRIBUTION_OFFLINE=1` | 否 | 离线自检，不发真实请求 |

---

## 7. 案例材料要准备什么

`selection/` 下两个文件，**均不含 GT**：

```json
// READY.json
{
  "ready": true,
  "case_count": 60,
  "prediction_manifest": "prediction_manifest.json",
  "prediction_manifest_sha256": "<sha256>",
  "evidence_root": "D:/data/evidence",
  "q85_cache_root": "D:/data/q85_image_cache"
}
```

```json
// prediction_manifest.json 的一条
{
  "case_id": "case_024",
  "identity": { "app": "...", "task_id": "...", "attempt": "..." },
  "inventory": [
    { "source_id": "S0001", "source": "images/a.png", "kind": "image", "sha256": "..." },
    { "source_id": "S0002", "source": "logs/step_1.txt", "kind": "log", "step": 1, "sha256": "..." }
  ],
  "images": [
    { "source_id": "S0001", "delivery_path": "D:/data/q85/xxx.webp", "delivery_sha256": "..." }
  ],
  "evidence_root": "D:/data/evidence/case_024",
  "available_steps": 5,
  "image_scope": "website"
}
```

`inventory` 是送达判定的唯一依据；`images[].delivery_path` 指向 q85 缓存，记录器按它替换字节。

---

## 8. 产物

| 文件 | 用途 |
|---|---|
| `prompt.txt` | 实际下发的提示词（含 schema） |
| `input_index.json` | 模型可见的全部日志页与图片 |
| `workspace/` | 沙箱（logs / images / .agents/skills） |
| `calls/NNN/responses_public.json` | 每次上游请求（图片只留 `uri_sha256` + 字符数） |
| `calls/NNN/route.json` | 本轮用途（main_agent / aux）与上游模型 |
| `upstream_audit.json` | 调用账本：是否真发上游、字节数、HTTP 状态、图片替换记录 |
| `access_receipt.json` | 证据送达回执：读了哪些页、看了哪些图 |
| `answer.txt` / `result.json` / `status.json` | 原始输出 / 编译结果 / 状态机 |

---

## 9. 实测记录

一条 5 步 / 5 图 / 14 条日志的案例（`case_024`），单卡单进程：

```
returncode     0            timed_out      false
coverage       19/19 源，5/5 图，3/3 页
上游调用        13 次
耗时           241.7 秒
state          validated（schema 与业务校验均通过）
图片替换        15 次，全部命中 q85 字节
```

---

## 10. 设计上几个刻意的选择

| 选择 | 原因 |
|---|---|
| 技能放进 `.agents/skills/` + `skip_host_skill_discovery=true` | 只让模型看到被测版本，排除本机已装技能污染 |
| 日志按 14k 字符切页并加标记 | 只有每一片都送达才算完整，读一次 ≠ 读全了 |
| 图片统一替换为 q85 WebP（原始尺寸） | 与历史实验字节一致，且把上下文从几十 MB 降到几 MB |
| `max_calls=160`、`retries=0`、wall 截止 | 调用次数与重试都要可复现，不靠重试掩盖失败 |
| 校验器从技能目录加载但不在沙箱内执行 | 模型改不到校验逻辑 |

---

## 11. 已知限制

- Windows 专属；`--approve-for-me` 与 `-c permissions.*` 依赖 codex 0.162.1
- 沙箱内禁用网络（刻意）
- 单案例 wall 1800s 是经验值，超时记 `runtime_failed`，不重试
- 上游网关需原生支持 Responses（基础请求、流式、reasoning 回传、并行工具调用）
