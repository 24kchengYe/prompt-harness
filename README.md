# Prompt Harness

[![GitHub](https://img.shields.io/badge/GitHub-24kchengYe%2Fprompt--harness-181717?logo=github)](https://github.com/24kchengYe/prompt-harness)
[![Version](https://img.shields.io/badge/version-0.10.0-176a5a)](https://github.com/24kchengYe/prompt-harness)
[![License](https://img.shields.io/badge/license-MIT-b54e32)](LICENSE)
[![Runtime](https://img.shields.io/badge/runtime-Python%203.10%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)

**把散落在 Claude Code 和 Codex 会话中的人类提示词与完整 Agent 执行轨迹，变成每个项目私有、可追溯、可检索的事实层。**

Prompt Harness 同时提供实时 `UserPromptSubmit` Hook、旧任务轮末恢复和自动历史对账。第一次从项目根目录开始对话时，它会自动创建 `.prompt-harness`，先保存当前输入，再在独立后台进程中检查并补齐启动目录恰好等于该项目根目录的 Claude Code 与 Codex 会话。父项目不会自动吸收从子目录启动的会话；确需跨根归属时可显式绑定。它分别保留用户真正输入的指令、用户发送的图片，以及模型可见输出、reasoning/thinking、工具调用与结果、系统/开发者注入和 subagent 内容；导入镜像仍会去重。

这个事实层将作为后续 badcase 分析、可复现测试和跨模型回归的稳定输入。

> 当前版本完成 Phase 1：提示词捕获与事实化。Phase 2 的 badcase harness 已预留命名空间，但尚未实现失败分类、测试运行和模型回归。

## 它解决什么问题

一次真实项目往往分散在多个 Claude、Codex、分支会话和导入归档里。直接分析原始 JSONL 会遇到几个问题：

- 人类输入和助手回复、工具调用、环境注入混在一起；
- Claude 分支会复制历史消息，Codex 导入 Claude 后还会形成镜像；
- 用户很难按项目重新找到“我当时到底要求 AI 做了什么”；
- 会话总结会变化，不能与原始提示词事实混在同一个文件里；
- 后续 badcase 测试需要稳定 ID，而不是依赖某个聊天窗口仍然存在。

Prompt Harness 将这些内容整理为：

```text
项目 → 会话 → 用户提示词 event_id → 关联的 Agent trace_event_id
```

每条事实记录都可以包含平台、时间、会话、模型、来源和哈希；总结与可视化则是随时可以重建的派生视图。

## 核心能力

| 能力 | 说明 |
|---|---|
| 实时捕获 | 通过 `UserPromptSubmit` Hook 在用户提交提示词时快速追加一条事件 |
| 长任务升级兼容 | Hook 每次运行时解析当前可用插件副本；长时间打开的 CLI/Desktop 任务不会因旧版本缓存被刷新而报错 |
| 自动引导与对账 | 没有账本时首次全量发现；之后每条输入只检查并读取发生变化的会话文件尾部 |
| 精确项目隔离 | 自动归属要求会话启动目录与项目根目录完全相同；父项目不吸收子目录会话 |
| 会话项目绑定 | 多根工作区、旧任务或错误 `cwd` 可显式绑定到一个项目；切换和迁移保持追加式审计 |
| 历史回填 | 扫描本地 Claude Code 与 Codex JSONL，恢复当前项目的历史人类输入 |
| Agent 轨迹归档 | 从本地 transcript 提取 assistant 文本、reasoning/thinking、工具调用与结果、系统/开发者注入和 subagent 内容，按统一事件格式生成 `MODELOUT.md` |
| 跨平台标记 | 每条记录明确区分 `claude` 与 `codex` |
| 模型元数据 | 优先使用 Hook 捕获值；历史记录可从 Claude assistant 行或 Codex `turn_context` 可靠推导 |
| 图片归档 | 保存用户发送的常见栅格图片，按内容哈希去重并嵌入 `PROMPTS.md`；不联网下载 |
| 文件附件 | 普通文件不复制正文，只在能解析时把附件路径保留在提示词事实中 |
| 去重与镜像排除 | 合并 Claude 分支历史副本，排除导入 Codex 的 Claude 镜像，同时保留真正的 Codex 续写 |
| 事实与总结分离 | `PROMPTS.md` 与 `MODELOUT.md` 只记录事实；可变化的会话与项目总结单独放在 `reports/` |
| 离线可视化 | 生成单文件 `timeline.html`，支持会话节点、搜索、Claude/Codex 筛选和提示词展开 |
| 隐私保护 | 默认不提交提示词与图片数据，省略普通附件正文并遮盖常见密钥、Token 和密码形态 |
| 可验证性 | `doctor` 检查事件 ID、哈希、项目归属、隐私清洗和 JSONL 完整性 |

## 工作方式

```mermaid
flowchart LR
  U["用户新提示词"] --> H["UserPromptSubmit Hook"]
  BIND["session → project 绑定"] --> N
  H --> D["后台自动对账调度"]
  D --> B["首次全量发现"]
  D --> J["增量 JSONL 尾部"]
  C["Claude Code JSONL"] --> B
  X["Codex rollout JSONL"] --> B
  C --> J
  X --> J
  H --> N["清洗与项目归属"]
  B --> N
  J --> N
  N --> E["append-only events/*.jsonl"]
  B --> O["append-only model-events/*.jsonl"]
  J --> O
  N --> A["assets/images + manifest.jsonl"]
  E --> M["事实 PROMPTS.md"]
  O --> MO["事实 MODELOUT.md"]
  A --> M
  E --> S["可变会话总结"]
  E --> V["离线 HTML 时间线"]
  E -. "event_id" .-> Q["未来 badcase 测试"]
```

Hook 前台路径保持有界：先验证原生会话 `cwd` 与候选项目根目录完全相同（或存在显式绑定），再自动初始化、清洗并追加当前提示词；如有图片，再校验并复制到本地内容哈希路径。随后它启动独立后台进程，不等待对账完成，也不联网或调用模型。项目第一次启用时做一次全量发现；之后每条输入都立即对账，但只比较已知源文件的大小与修改时间，并从保存的字节游标读取新增 JSONL 行。项目锁合并同项目重叠请求，全局锁避免多个项目同时重扫磁盘。

用户主目录也可以作为一个精确项目根。例如从 `C:\Users\ASUS` 直接启动的通用会话会写入 `C:\Users\ASUS\.prompt-harness`；从其任何子目录启动的未绑定会话不会进入这本账。文件系统根目录（如 `C:\`）仍被拒绝。

## 从公开 GitHub 安装到 Codex

仓库地址：[github.com/24kchengYe/prompt-harness](https://github.com/24kchengYe/prompt-harness)

Codex CLI 可以直接把这个公开仓库注册为远程 marketplace：

```powershell
codex plugin marketplace add 24kchengYe/prompt-harness --ref main
codex plugin add prompt-harness@24kchengye
```

安装后请：

1. 新建或重新打开一个 Codex 任务；正在运行的旧任务不会热加载刚安装的 Hook。`0.7.0+` 创建的任务在后续插件升级后会自动前滚到当前可用运行时。
2. 在 Codex 中运行 `/hooks`，检查并信任 Prompt Harness 的 `UserPromptSubmit` Hook。
3. 在目标项目里发送一条测试提示词。
4. 用后文的 `doctor` 命令验证是否已写入。

查看安装状态：

```powershell
codex plugin list
```

更新远程 marketplace 和插件：

```powershell
codex plugin marketplace upgrade 24kchengye
codex plugin add prompt-harness@24kchengye
```

更新后 Hook 定义的哈希会变化，请在 `/hooks` 中重新检查并信任。若某个任务创建于 `0.7.0` 之前且旧缓存已经被清理，它仍可能继续引用已不存在的脚本路径；重开任务即可加载稳定启动器，或者使用后文的旧任务 `Stop` 恢复路径。

卸载插件：

```powershell
codex plugin remove prompt-harness@24kchengye
codex plugin marketplace remove 24kchengye
```

## 从源码安装或开发

```powershell
git clone https://github.com/24kchengYe/prompt-harness.git
cd prompt-harness
```

如果你已经维护自己的本地 Codex marketplace，可以把本仓库作为一个本地插件源注册；本仓库根目录包含 `.codex-plugin/plugin.json`，远程 marketplace 清单位于 `.agents/plugins/marketplace.json`。

本项目运行时仅使用 Python 标准库，不需要 `pip install`。要求 Python 3.10 或更高版本。

## 为 Claude Code 安装 Hook

Codex 推荐使用上面的插件方式。Claude Code 使用仓库内的安全安装器：

```powershell
# 先预览，不修改配置
python scripts/install_hooks.py --platform claude --dry-run

# 确认后安装
python scripts/install_hooks.py --platform claude
```

macOS/Linux 可将 `python` 替换为 `python3`。

安装器会：

- 保留已有的无关 Hook；
- 在实际修改前创建带时间戳的配置备份；
- 只写入 Prompt Harness 的 `UserPromptSubmit` 项；
- 不读取或上传认证信息。

移除 Claude Code Hook：

```powershell
python scripts/install_hooks.py --platform claude --remove
```

不要同时启用 Codex 插件 Hook 和 Codex 全局独立 Hook，否则同一条提示词可能被捕获两次。

## 快速开始

你可以直接在 Codex 中用自然语言要求插件技能执行：

```text
为当前项目初始化 Prompt Harness。
回填这个项目所有 Claude Code 和 Codex 的用户提示词并校验。
搜索我之前关于 major revision 的提示词。
```

也可以直接使用 CLI。

### 自动模式

安装并信任 Hook 后，无需先手动运行 `init` 或 `backfill`：

1. 从项目根目录启动或打开一个新任务/旧任务并发送第一条提示词；
2. Prompt Harness 自动创建项目目录并同步保存当前输入；
3. 首次后台发现所有“启动目录恰好等于项目根”的 Claude/Codex 历史，之后只增量读取变化的会话文件；
4. 结果写入 `.prompt-harness/state/auto-sync.json`，可用 `doctor` 检查。

当前输入每次都会实时保存并触发后台校验，没有五分钟节流。每轮模型输出结束时，`Stop` Hook 会再次读取本轮追加内容，因此 `modelout/<session>.md` 和 `trajectory/<session>.md` 无需等到下一次用户输入才补齐。`trajectory` 是可继续会话的实时快照：Stop 表示当前 turn 已闭合，不表示该 session 永久结束。会话索引用 `closed` 或 `open_or_interrupted` 描述最新 turn，不使用不可逆的 session completed 状态。

为避免项目级完整轨迹大到无法打开：

- `index/MODELOUT.md` 聚合完整的 assistant 最终回答；
- `index/TRAJECTORY.md` 按 turn 聚合完整的人类 Prompt 和完整最终回答；
- reasoning、tool call/result、系统/开发者注入和 subagent 内容在项目级轨迹中只显示分类数量；
- 完整事实仍按会话保存在 `index/modelout/` 与 `index/trajectory/`。

不再额外生成 `MODELOUTEASY.md`、`TRAJECTORYEASY.md` 或项目级完整过程副本。

`.prompt-harness/state/source-cursors.json` 记录每个源文件的大小、修改时间、字节偏移和行号；未变化文件不解析。增量同步还会只按文件名枚举 Codex 的全局 rollout 目录，并打开此前未知的候选文件，以发现同一启动目录下后来创建的其他会话。只有游标尚未建立、显式 `auto-sync --force`，或单个源文件被截断/改写时才降级为更重的读取。

若任务从 `<project>\child` 启动，而 `child` 本身不是独立项目根，它不会写入父项目 `<project>\.prompt-harness`。若 `child` 有自己的 `.git`、`AGENTS.md`、`CLAUDE.md` 等项目标记，它可以作为独立根创建自己的 Harness。需要有意把该会话归到父项目时，使用 `bind-session --migrate`。

### 1. 初始化项目

```powershell
python scripts/prompt_harness.py init --project "G:\path\to\project"
```

这会在目标项目根目录创建 `.prompt-harness/`，不会把提示词写回插件仓库。

### 2. 回填历史提示词并生成视图

```powershell
python scripts/prompt_harness.py backfill `
  --project "G:\path\to\project" `
  --platform all `
  --rebuild-index
```

macOS/Linux：

```bash
python3 scripts/prompt_harness.py backfill \
  --project "/path/to/project" \
  --platform all \
  --rebuild-index
```

### 3. 搜索用户提示词

```powershell
python scripts/prompt_harness.py search "major revision" `
  --project "G:\path\to\project" `
  --limit 20
```

需要机器可读结果时增加 `--format json`。

### 4. 显式绑定或迁移一个会话

当一个 Codex 任务打开多个工作区、原始 `cwd` 不可靠，或会话曾被写进错误项目时，用原生会话 ID 固定归属：

```powershell
python scripts/prompt_harness.py bind-session `
  --platform codex `
  --session-id "<native-session-id>" `
  --project "D:\PHD\historyCMAB" `
  --migrate
```

`--migrate` 只读取该会话的原生 transcript，把缺失提示词和图片补到目标项目；若同一事件已存在于其他已注册项目，只追加 `event-exclusion` 使旧位置不再出现在有效视图中，不删除原始 JSONL。若 transcript 不在默认 Claude/Codex 目录，可增加 `--source-path "<transcript.jsonl>"`。

查看当前生效绑定：

```powershell
python scripts/prompt_harness.py list-bindings
```

再次把同一会话绑定到别的项目会追加一条新记录，最新绑定生效，旧绑定仍留作审计。

### 5. 重建 Markdown、总结和 HTML

```powershell
python scripts/prompt_harness.py rebuild-index --project "G:\path\to\project"
```

### 6. 校验项目事实库

```powershell
python scripts/prompt_harness.py doctor --project "G:\path\to\project"
```

一次正常结果类似：

```json
{
  "ok": true,
  "event_count": 390,
  "active_event_count": 388,
  "superseded_event_count": 2,
  "excluded_event_count": 0,
  "image_count": 12,
  "active_image_count": 12,
  "image_file_count": 11,
  "auto_sync": {
    "status": "completed"
  },
  "errors": [],
  "warnings": []
}
```

### 7. 显式修复旧账本

当新版本扩展了密钥识别或自动上下文过滤规则时，可以显式修复已经生成的本地账本：

```powershell
python scripts/prompt_harness.py scrub-secrets --project "G:\path\to\project"
python scripts/prompt_harness.py clean-store --project "G:\path\to\project"
python scripts/prompt_harness.py doctor --project "G:\path\to\project"
```

`scrub-secrets` 只重新遮盖新识别出的敏感值；`clean-store` 会移除中断通知、重复的 Codex goal 续跑包装等非人类记录，并把附件包装压缩为“用户文字 + 引用路径”。这两个命令不会自动运行，修复时保留原有 `event_id`，随后重建派生视图。

### 8. 兼容安装插件前已经存在的 Codex 任务

部分旧任务会保留创建时的插件 Hook 集合。若新任务的 `UserPromptSubmit` 正常、旧任务仍不触发，可安装轻量的轮末恢复 Hook：

```powershell
python scripts/install_hooks.py --platform codex --codex-hook stop-recovery
```

它在每轮结束后根据该任务的 `session_id` 只读取对应 rollout 的最后一条人类输入，记录为 `Source mode: stop_recovery`，随后触发同样的首次全量/后续增量对账。即使项目原来没有 `.prompt-harness`，也会自动建立。它可以与插件的即时 Hook 共存；相同 `turn_id + prompt hash` 会阻止重复写入，同时允许一个 turn 中存在多条不同的人类消息。安装后仍需在 `/hooks` 中检查并信任新增命令。

## 每个项目会生成什么

```text
<project>/.prompt-harness/
├── config.json                       # 项目标识与隐私策略
├── events/YYYY/MM/prompts-*.jsonl    # 追加写入的事实源
├── model-events/YYYY/MM/model-outputs-*.jsonl # 追加写入的结构化 Agent trace
├── assets/
│   ├── images/<sha256>.*              # 用户发送的内容寻址图片
│   └── manifest.jsonl                 # 图片与 event_id 的追加式关系
├── sessions/
│   ├── claude/*.json                 # Claude 会话派生元数据
│   └── codex/*.json                  # Codex 会话派生元数据
├── index/
│   ├── catalog.json                  # 数量、平台和时间覆盖
│   ├── sessions.json                 # 会话分组
│   ├── PROMPTS.md                    # 纯事实提示词 Markdown
│   ├── MODELOUT.md                   # 逐条 Agent trace 事实 Markdown
│   ├── TRAJECTORY.md                 # 本项目全部会话的分段交互轨迹
│   ├── prompt/*.md                   # 每个会话独立的提示词文件
│   ├── modelout/*.md                 # 每个会话独立的模型/Agent trace 文件
│   └── trajectory/*.md               # 每个会话独立的完整交互轨迹
├── reports/
│   ├── SESSION_SUMMARIES.md          # 提示词派生、可变化的会话摘要
│   └── PROJECT_SUMMARY.md            # 可选的项目分析总结
├── visualizations/
│   └── timeline.html                 # 单文件离线时间线
├── state/                            # 写入锁与诊断状态
│   ├── auto-sync.json                # 最近自动对账状态与结果
│   ├── source-cursors.json            # 各会话源文件的增量读取游标
│   ├── source-models.json             # 按源文件指纹缓存的模型派生结果
│   ├── index-dirty.json               # 派生视图是否需要重建
│   ├── auto-sync-pending.json         # 重叠触发合并队列
│   ├── event-supersessions.jsonl      # 追加式旧事件替代关系
│   └── event-exclusions.jsonl         # 追加式非人类事件排除关系
└── badcases/                         # Phase 2 预留空间
```

### 事实层与派生层

| 类型 | 文件 | 规则 |
|---|---|---|
| 权威事实 | `events/**/*.jsonl` | 日常写入 append-only；原始事实不因迁移重复而删除 |
| Agent trace 事实 | `model-events/**/*.jsonl` | 保存带类型的文本与结构化 payload，并用 `prompt_event_id`、`tool_call_id`、父会话元数据建立关联 |
| 事实补偿 | `state/event-supersessions.jsonl` | 指明旧事件由哪个规范事件替代，视图只展示有效事件 |
| 事实排除 | `state/event-exclusions.jsonl` | 标记旧版本误收的自动上下文，不删除原始行 |
| 图片事实 | `assets/images/`、`assets/manifest.jsonl` | 图片按哈希存储；关系按 `event_id` 追加 |
| 可读事实 | `index/PROMPTS.md` | 只有最小标题、逐条元数据和完整清洗后提示词 |
| 可读 Agent trace | `index/MODELOUT.md` | 按 `O00001` 排列完整轨迹事件，显示事件类型、actor、subagent、结构化 payload 与对应 `P` 编号 |
| 项目会话轨迹 | `index/TRAJECTORY.md` | 文件开头统计总 Session、Claude/Codex Session、总 Turn、总 Prompt；逐会话表列出各自 Turn/Prompt/trace 数，再按 `platform + session_id` 隔离展示问答 Turn |
| 逐会话文件 | `index/{prompt,modelout,trajectory}/*.md` | 三个目录使用同一会话文件名：`时间-platform-model-会话主题.md`；其中 trajectory 按 Prompt-first Turn 展示，若名称冲突则追加稳定短哈希 |
| 派生索引 | `catalog.json`、`sessions.json` | 可以重建 |
| 可变总结 | `reports/*.md` | 允许随新会话改变结论 |
| 可视化 | `visualizations/timeline.html` | 可以重建，不是事实源 |

每条事件都带有稳定的 `event_id`。未来 badcase 记录只引用这个 ID，不复制或修改原始提示词。

轨迹中的统一 `turn_id` 是规范化字段：Codex 直接使用 rollout 原生
`turn_id`；Claude 沿 `parentUuid` 消息链回溯到对应的人类 user 行，使用
该行的 `promptId`，缺失时退回 `uuid`。

`P00001` 这类编号是当前有效提示词按 `occurred_at` 排列后的派生顺位，不是永久身份。历史回填发现更早记录时，后续 P 编号会自动重排；对应事件的 `event_id` 保持不变。时间完全相同时，系统使用来源文件、行号和原生消息标识作确定性排序。

## 一条提示词记录包含什么

可读 Markdown 中的单条记录类似：

````markdown
## P00042

- Time: `2026-07-14T06:08:33.759Z`
- Platform: `codex`
- Model: `gpt-5.6-sol`
- Session: `...`
- Event ID: `phe_...`
- Source mode: `hook`
- Images: `1`

```text
用户实际输入的提示词
```

![P00042 image 1](../assets/images/<sha256>.png)
````

平台始终显示。模型只在有可靠证据时显示：

- 实时 Hook：使用 Hook 负载中的模型字段；
- Claude 历史：从该用户消息后续的 assistant 模型字段推导；
- Codex 历史：从该轮 `turn_context` 推导；
- 无可靠来源：显示 `unavailable`，不猜测；
- `<synthetic>` 等内部占位值不会被当作模型名。

## 哪些内容会保存

保存：

- 用户实际输入的提示词文本；
- Claude/Codex 平台标记；
- 会话、轮次、时间、项目根目录和来源引用；
- 可获得的模型与权限模式；
- SHA-256、清洗统计和未来 badcase 链接。
- 用户发送的 PNG、JPEG、GIF、WebP、BMP 图片，以及图片与提示词事件的关系。
- Claude/Codex 发给用户的人类可见文本输出，以及它关联的提示词 `event_id`。

Agent trace 不按类别排除：

- reasoning、thinking 和可恢复的加密/摘要字段按原生结构保留；
- 工具调用、工具结果和终端输出保留为 `tool_call` / `tool_result`；
- subagent、sidechain、系统/开发者注入和运行时上下文保留，并明确标注来源与父会话；
- 明显密钥和内嵌二进制仍递归脱敏，Claude-to-Codex 导入镜像仍去重。
- 注入的 `AGENTS.md`、环境、权限与续写包装；
- Codex 的 `turn_aborted`、内部建议生成和重复 goal 续跑包装；
- 普通文件、文档及其他非图片附件正文；
- Codex 中仅仅镜像 Claude 历史的导入行。

如果用户在提示词中手动写了文件路径，或者普通附件块中能解析出本地路径，路径文本会保留；Prompt Harness 不会为了归档而打开这个普通文件并复制正文。图片是明确例外：用户发送的常见栅格图片会保存到 `.prompt-harness/assets/images/`，远程 URL 不下载，SVG 不保存。

## 去重原则

Prompt Harness 不会简单地按文本去重，因为用户可能在不同时间有意重复同一句话。

- Claude 分支复制：按原生事件、时间戳和提示词哈希识别历史副本，并保留全部来源引用；
- 回填身份匹配：优先使用原生消息 ID、turn ID、源文件路径与行号，再使用平台、会话和文本出现次数；
- Codex 导入镜像：导入时间范围内的 Claude 镜像不重复计入；
- 旧 Codex 任务：可通过 `capture-stop-recovery` 在该轮结束后只读取本任务 rollout 的最后一条人类输入；
- 自定义数据目录：自动同步和 Stop 恢复会读取 `CODEX_HOME`，Claude 发现会读取 `CLAUDE_CONFIG_DIR`；这对 Windows 非默认安装目录尤其重要；
- Windows 上转发 Stop payload 时固定使用 UTF-8；若复用已有 Stop adapter，应使用 `ensure_ascii=True` 序列化，并为子进程显式设置 `encoding="utf-8"` 与 `PYTHONIOENCODING=utf-8`，避免 GBK 遇到孤立 Unicode 字符后漏记；
- Codex 真实续写：超过原 Claude 会话结束时间的新用户输入仍然保留；
- 实时重复输入：如果用户确实提交了两次，则保留两个事件；
- 回填与 Hook 对账：按平台、会话和提示词出现次数避免再次写入已捕获事件。
- 旧格式迁移：不删除旧 JSONL 行，而是追加 supersession 关系，让事实视图只展示新版规范事件。

## HTML 时间线

`visualizations/timeline.html` 是一个完全自包含的本地文件：

- 不需要服务器、数据库、CDN 或网络连接；
- 按项目和会话排列提示词节点；
- Claude 与 Codex 使用不同颜色；
- 支持全文搜索和平台筛选；
- 点击节点可查看原始用户提示词及模型来源；
- 鼠标经过轨道时会产生轻量波动；
- 支持键盘焦点、小屏布局和 `prefers-reduced-motion`。

HTML 时间线当前仍以提示词为主；逐条 Agent trace 写入 `index/MODELOUT.md`，按会话组织的完整交互写入 `index/TRAJECTORY.md`。

## 隐私与发布边界

提示词可能包含未发表研究、私人路径和认证信息。因此默认策略是：**代码可以公开，项目提示词库保持私有。**

每个 `.prompt-harness/` 内置嵌套 `.gitignore`，默认排除：

- `events/`
- `sessions/`
- `index/`
- `reports/`
- `assets/`
- `visualizations/`
- `state/`
- badcase case/run 数据

此外：

- 常见 API Key、access token、password 和 bearer token 会被替换；
- 用户图片只保存在私有的 `assets/images/`；普通文件正文与非图片 base64 载荷会省略；
- 全局注册表 `~/.prompt-harness/projects.json` 只记录项目位置，不存提示词；
- 会话绑定表 `~/.prompt-harness/session-bindings.jsonl` 只记录平台、会话 ID、项目路径、可选 transcript 路径和绑定时间，不复制提示词；
- 任何公开导出前都应运行 `doctor`，并人工检查或使用 secret scanner。

自动遮盖只是安全网，不等于任意秘密都能被百分之百识别。详见 [PRIVACY.md](PRIVACY.md)。

## 常见问题

### 安装后为什么没有实时记录？

先运行 `/hooks` 并信任 Hook，然后新建任务做基准测试。已经运行的旧任务通常不会热加载后来安装的插件；重启后重新打开能否补挂 Hook 还取决于 Desktop 版本和任务来源，Claude 导入任务尤其应实测。旧任务中遗漏的输入可以通过 `backfill` 补回。

### 为什么记录显示 `Source mode: backfill`？

说明它来自历史 JSONL 扫描，而不是提交瞬间的 Hook。它仍然是有效事实，并保留原始文件与行号来源。

### 多根工作区或错误 cwd 导致账本落错位置怎么办？

运行 `bind-session --migrate`。此后该会话的 Hook、Stop 恢复和历史对账都会优先使用显式项目绑定；迁移会重建两侧派生视图，但只追加事实与排除关系，不删除原始事件。

### 为什么从项目子目录启动的会话没有进入父项目？

这是精确根隔离的预期行为。自动归属只接受“会话启动目录 = 项目根目录”，防止一个大目录吞掉下面多个独立项目。请从目标项目根目录启动任务；如果这个跨根归属是有意的，再显式运行 `bind-session --migrate`。

### 为什么模型显示 `unavailable`？

该条记录没有可靠模型字段，或原始日志无法访问。Prompt Harness 不会根据会话标题猜模型。

### 为什么同一条提示词出现两次？

先确认用户是否真的提交了两次。如果是实时重复输入，应当保留。若事件来源都为 Hook，则检查是否同时启用了插件 Hook 和 Codex 全局独立 Hook。

### 为什么历史数量明显过多？

检查 Claude 分支复制或 Claude-to-Codex 镜像是否重复。subagent 与自动注入现在是预期 trace 事实，应通过事件类型和会话字段识别，而不是排除。

### HTML 没有更新怎么办？

重新执行：

```powershell
python scripts/prompt_harness.py rebuild-index --project "<project-root>"
```

### Hook 会拖慢每次对话吗？

前台 Hook 只做本地文本清洗、追加、有界图片复制和后台进程调度。首次历史发现或后续增量读取都在独立进程运行，不阻塞当前回答，不联网，也不调用模型。普通轮次只读取发生追加的会话 JSONL 尾部；未新增事件或图片时跳过重复视图重建，模型推导结果也按源文件指纹缓存；多个项目的重任务通过全局锁串行化。

## 故障排查顺序

当 Hook 没有记录时，建议依次检查：

1. `codex plugin list` 中插件是否为 `installed, enabled`；
2. `/hooks` 中 `UserPromptSubmit` 是否已信任；
3. 当前任务是否在安装或信任之后新建；
4. Hook 负载中的 `cwd` 是否能定位到正确项目；
5. 项目下是否生成 `.prompt-harness/state/hook-misses.jsonl`；
6. 查看 `.prompt-harness/state/auto-sync.json` 的 `status`、`last_error` 和 `last_result`；
7. 运行 `backfill` 和 `doctor`，判断是实时链路问题还是事实库问题。

不要通过直接改写 canonical JSONL 来“修复”计数。优先修复采集逻辑、追加补偿事件，或执行有来源记录的迁移。

## 项目边界

当前版本会做：

- 提示词与完整 Agent trace 的捕获/回填、清洗、去重和 Markdown 索引；
- 首次对话自动初始化并发现全项目历史，后续每次输入执行游标式增量对账；
- 为未来 badcase 提供稳定 `event_id` 和项目级目录结构。

当前版本不会做：

- 不会评判模型输出质量；reasoning、工具调用与结果只按本地 transcript 中实际可恢复的内容归档；
- 自动认定一次会话是否成功；
- 上传提示词到云端；
- 读取用户提示词中引用的文件正文；
- 下载远程图片 URL 或保存 SVG；
- 自动发布项目提示词数据；
- 执行 Phase 2 的 badcase 回归测试。

## Badcase Harness 路线图

未来 badcase 层计划通过 `event_id` 引用提示词，并增加：

```text
badcases/cases/<case-id>/
├── case.json                 # 失败定义与来源事件
├── analysis.md               # 根因分析
├── fixtures/paths.json       # 只保存测试所需路径映射
├── acceptance.json           # 可执行验收标准
└── runs/<model>/<run-id>.jsonl
```

目标流程是：发现长期无法解决的问题 → 固化 badcase → 定义验收测试 → 对指定模型反复运行 → 形成可复现的解决流程与回归记录。

详见 [references/badcase-roadmap.md](references/badcase-roadmap.md)。

## 开发与验证

```powershell
python -m unittest discover -s tests -v
python -m py_compile scripts/prompt_harness.py scripts/install_hooks.py
python scripts/prompt_harness.py doctor --project "<test-project>"
```

运行时仅依赖 Python 标准库。当前实现支持 Windows、macOS 和 Linux，Windows + PowerShell 路径经过了更充分的实际测试。

## 设计文档

- [事件结构](references/event-schema.md)
- [架构与去重](references/architecture.md)
- [隐私模型](PRIVACY.md)
- [Badcase 路线图](references/badcase-roadmap.md)
- [相关会话历史工具](references/related-work.md)
- [Prompt Harness Skill](skills/prompt-harness/SKILL.md)

## 远程仓库

- GitHub：[24kchengYe/prompt-harness](https://github.com/24kchengYe/prompt-harness)
- 默认分支：`main`
- Marketplace：`24kchengye`
- Plugin：`prompt-harness@24kchengye`
- 当前版本：`0.6.0`
- 可见性：Public
- License：[MIT](LICENSE)

欢迎通过 [Issues](https://github.com/24kchengYe/prompt-harness/issues) 报告 Hook 兼容性、历史回填和去重问题。提交问题时请先移除真实提示词、私人路径和认证信息。
