# video-agent：分镜规划与保守恢复的执行器

正式开发目录：`C:\Users\ym\Documents\GitHub\video-agent`。后续 Trae 和 Codex 都应打开此目录。
需要 Python 3.10 或更新版本；现有 Mock 示例和离线测试只使用标准库，无需 Key 或新增依赖。

当前已实现 Planner、结构化分镜校验、Mock/Real 状态隔离及顺序 Executor。
本轮只做离线安全验证，**没有验证真实 LLM、图片或视频生成成功**。
Mock 的 `.mp4` 是文本占位文件，不是可播放视频。

## 架构与职责

```text
video-agent/
  main.py                         # CLI、参数检查、规划/读计划、选择执行模式
  config.py                       # 真实调用时按需读取本项目配置
  agent/
    planner.py                    # 提示词、时长推断、JSON 解析与严格校验
    executor.py                   # 身份校验、提交意图、查询、下载及恢复
    __init__.py
  tools/
    _http.py                      # 原 HTTP 协议；收费 POST 不重试、不跟随重定向
    llm.py                        # Seed 文本工具
    image_generator.py            # Seedream 图片工具，当前执行流程未使用
    video_generator.py            # MiniMax 单次提交/查询；明确模型 ID 可固定请求
    __init__.py
  examples/future_city_10s.json     # 人工编写的两个 5 秒镜头
  tests/
    test_planner.py
    test_executor.py
    test_safety_and_tools.py
    __init__.py
  .env.example
  .gitignore
  requirements.txt
  README.md
  runs/                           # 按需创建；被 Git 忽略
    mock/                         # Mock 默认状态和占位文件
    real/                         # Real 默认状态和真实片段
```

Planner 将需求和提示词交给 LLM，再将返回文本校验为 `VideoPlan`；注入 Mock 响应时走相同解析与校验。
Executor 接收验证后的计划，按镜头顺序执行后端，不重新规划，也不负责拼接。
`MockBackend` 完全离线；`RealBackend` 使用明确的视频模型 ID，状态标识与实际提交使用同一个 ID。
接口、外部响应字段和原有生成参数沿用现有工具，没有更换服务或虚构外部模型 ID。

## 命令模式

| 参数 | 实际含义 |
|---|---|
| `--dry-run` | 只检查/打印本地计划或 Mock 响应；必须提供 `--input` 或 `--mock-response`，不请求 LLM、不执行任务 |
| `--run` | 默认执行 Mock；必须提供本地计划或 Mock 响应，不启动真实规划 |
| `--run --real` | 选择真实视频后端；需要明确交互确认，可能计费，本轮未运行 |
| `--run --dry-run` | 报错；在读取计划、配置、修改状态或发送请求前拒绝 |
| `--run --real --dry-run` | 同样报错，不能覆盖 Dry-run 的非执行约定 |
| `--real` 没有 `--run` | 报错 |
| 没有 `--run` 和 `--dry-run` | 默认只规划和预览；没有本地输入或 Mock 响应时调用真实 LLM，规划仍可能计费 |

真实执行的交互确认发生在规划之后。若使用真实模式且没有本地计划/Mock 响应，规划阶段的 LLM 本身可能先产生费用。
离线验收请始终使用下面的固定示例或已保存的分镜文件。

## 离线 Dry-run

```powershell
cd 'C:\Users\ym\Documents\GitHub\video-agent'
python -B main.py --dry-run --mock-response examples/future_city_10s.json --prompt "生成一个10秒的未来城市科幻视频"
```

输出合法的 10 秒、16:9、两个 5 秒镜头 JSON，并显示两次计划中的视频工具调用与实际提交次数 0。
`--mock-response` 是固定的人工示例，不会根据任意新输入智能生成新分镜；仍会核对明确的需求时长。

## 保存计划、执行 Mock 与恢复

为每次独立试验创建唯一目录，避免覆盖已有任务数据：

```powershell
$mockRun = Join-Path 'runs/mock' ('demo-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $mockRun -ErrorAction Stop | Out-Null
python -B main.py --dry-run --mock-response examples/future_city_10s.json --prompt "生成一个10秒的未来城市科幻视频" --output "$mockRun/plan.json"
python -B main.py --input "$mockRun/plan.json" --run --state "$mockRun/state.json" --output-dir "$mockRun/videos"
```

`--output` 仅在非执行路径保存纯分镜 JSON，父目录须已存在，已有文件不会覆盖。
Mock 成功后有两条 `downloaded` 状态和 `shot_1.mp4`、`shot_2.mp4`；文件内容只是 `mock video clip` 文本。

恢复同一次运行时，保持计划、模式、模型、请求参数、状态和输出目录一致，再运行第二条命令。
新 PowerShell 窗口中须先把 `$mockRun` 指向原目录。
默认路径已分开：Mock 使用 `runs/mock/state.json`、`runs/mock/videos`；Real 使用 `runs/real/state.json`、`runs/real/videos`。
显式指定同一状态路径也无法跨模式恢复；程序会校验状态身份并拒绝。

## 分镜协议

顶层字段：`total_duration_seconds`、`video_style`、`aspect_ratio`、`shot_count`、`shots`。
镜头字段：`id`、`duration_seconds`、`description`、`video_prompt`。这些是本项目协议，不是外部 API 字段。

- 每镜头固定 5 秒，16:9；总时长是正整数，等于镜头数乘以 5。
- ID 从 1 连续递增，数组长度与镜头数相符，文本非空。
- 拒绝缺失/额外字段、重复 JSON 键、布尔值冒充整数、NaN、Infinity、截断文本和附加解释；允许单个完整 Markdown JSON 代码块。
- 可推断唯一的阿拉伯数字时长；不能组成 5 秒镜头的明确时长会在调用 LLM 前拒绝。
- 复杂表达不猜测，可用 `--duration 10` 指定；与明确需求冲突时拒绝。

格式正确不能保证画面内容质量或跨镜头一致性；校验失败不自动重试 LLM。

## 状态隔离与恢复校验

新状态协议为 `schema_version: 2`，保存：

- `execution`：模式、服务商、模型 ID、现有 API 基址，以及 `duration=5`、`resolution=768P`、`ratio=16:9` 的请求标识。
- `plan_hash`：包含全部镜头描述和 prompt 的计划哈希。
- `records`：各镜头状态、任务 ID、下载地址和本地路径。

不保存 API Key。恢复时严格核对模式、服务商、模型、接口基址、关键参数及其类型、计划哈希和记录结构。
缺少模式、版本或有效哈希的旧状态一律拒绝自动恢复，**保留原文件、不自动迁移或改写**。
旧状态可能有已付费任务 ID，必须人工向服务商核实后处理，不能直接补一个模式字段就当作可信状态。

Real 拒绝 Mock 任务 ID、`mock://` 地址和已知 Mock 占位文件；Mock 拒绝 Real 状态、非 Mock 任务 ID 和真实下载地址。
已完成记录会检查输出路径和非空文件是否存在；缺失时停止，不自动提交替代任务。
这不是完整的视频格式、质量或文件真实性验证。

## 提交意图与执行流程

Executor 先读取并验证状态。运行时创建独占 `.lock` 文件，再在锁内重新读取状态，避免用旧的内存快照提交。
其他进程或遗留锁存在时拒绝运行，不自动删除锁；正常退出仅清理当前运行自己创建的锁。

每个新镜头按以下流程执行：

1. **先保存 `submission_intent`**，写入独立临时文件、flush、`fsync`，再原子替换状态文件。
2. 只有意图成功落盘，才调用一次提交方法。写盘失败时停止，请求不会发出。
3. 有效任务 ID 返回后保存 `submitted`。返回丢失、异常或无效 ID 时停止；可落盘时保存 `submission_uncertain`。
4. 已有 ID 时查询原任务，不重新提交；成功且有下载地址时保存 `succeeded`，下载后保存 `downloaded`。
5. 查询失败/取消保存 `failed`；查询耗尽次数保留 ID。`succeeded` 恢复时只重试下载，`downloaded` 跳过。

遗留 `submission_intent`、`submission_uncertain` 或 `failed` 都会阻断自动重提交。
提交成功但保存 ID 失败时，磁盘上的提交意图仍会阻断下一次自动提交。
状态 JSON 损坏、无法读取、缺少已提交 ID、执行顺序异常或身份不匹配时，也不会退回“新任务”流程。

已有输出文件但缺少匹配的完成状态时停止，不覆盖文件。下载中断或下载后状态写入失败留下的文件需要人工核实。
真实后端查询间隔默认 5 秒、最多 60 次；可用 `--poll-interval`、`--max-polls` 指定。Mock CLI 的查询间隔为 0 秒。
收费 POST 不跟随重定向，代码没有 POST 自动重试循环；不确定结果交给人工核实。

## 保留任务数据与保证边界

任务状态、下载地址和已付费任务 ID 必须私下保留，不提交 Git；日志也可能含任务 ID，分享前应检查。
不要删除状态文件、切换到新的状态路径或清除锁来强迫重试收费任务。
进程被强制终止可能留下锁；先确认没有其他进程运行，再核实服务商任务及状态记录，由人工处理。

**本项目不保证绝对 exactly-once 计费。** 文件 flush/fsync、原子替换和锁可降低风险，但突然断电、存储故障、人工删改/回滚状态、切换账号，以及服务商是否支持幂等请求，都未获得线上验证。
本轮测试的“进程中断”是通过未被捕获的 BaseException 模拟，不是实际断电或服务商故障测试。
同一任务的状态和输出数据应备份并保持一致，未知提交结果不能自动重试。

## 离线测试与本轮结果

```powershell
python -B -m unittest discover -s tests -v
```

现有测试使用标准库 Mock、假后端、替换的 HTTP/配置函数及人工响应。`-B` 避免写字节码缓存。
测试使用 `TemporaryDirectory`；需要强制断网时，应在禁止网络的环境中运行。
上述命令自身不是全局网络阻断器，修改测试后仍应重新审查真实请求路径。

阶段 3A（2026-10-09）实际在仓库外使用临时进程级网络守卫运行原命令：**61 项通过，0 失败、0 错误、0 跳过**，比阶段二新增 32 项。
守卫在项目导入前阻断 socket、真实 HTTP/配置库、子进程、私密文件访问及目录外写入；临时文件全部位于独立审计目录。
实际测试和端到端验证网络请求尝试 0 次、真实 API 请求 0 次；守卫源码未加入项目。

覆盖包括：分镜合法性、导入无副作用、离线预览、原请求协议、两镜头 Mock 执行、已有 ID 不重提、查询/下载恢复、双向模式拒绝、旧状态保留并拒绝、模型/参数/计划不匹配、Mock ID/地址/占位文件拒绝、参数冲突提前拒绝、提交前写盘/fsync/替换失败、模拟中断、返回 ID 写盘失败、不确定结果写盘失败、状态损坏/无权读取、独占锁和 POST 不重试/不重定向。

另用 CLI 验证：示例规划 → 保存 JSON → Mock 执行两镜头 → 完成状态恢复；以及第一次查询中断后恢复，镜头 1 只查询，仅新提交镜头 2。
所有这些结果都是 Mock/离线验证，不代表真实视频生成、账号权限、线上协议或视频质量验收。

## 尚未验证与后续开发

尚未验证真实 LLM 输出、真实 MiniMax 提交/查询/下载、服务商幂等支持或实际收费行为。
尚未实现拼接、自动质量评估、反馈修改、模型训练、复杂多 Agent 系统或并行镜头调度。
没有新增框架、依赖或外部模型配置。

`.env.example` 只列出空配置项：`ARK_API_KEY`、`SEED_MODEL_ID`、`SEEDREAM_MODEL_ID`、`MINIMAX_API_KEY`、`MINIMAX_VIDEO_MODEL_ID`。
真实调用才读配置；环境变量优先于本项目 `.env`，不搜索 A/B 或原始 `video_img` 的配置。Mock 不需这些变量。

`.gitignore` 排除 `.env`、`.env.*`、虚拟环境、缓存、`runs/` 和生成媒体，允许 `.env.example`；不会自动移除已跟踪的私密文件。
首次提交前仍需核对 `git status --short` 和 `git ls-files`。
当前执行环境的原生 Git 进入 C 目录仍报权限错误；阶段 3A 未修改系统权限或重新初始化仓库，需要在普通 PowerShell 中复核。

未来计划用 `main` 保存验收版本、`dev` 开发、标签保存确认的基线。阶段 3A 未创建提交、分支、标签或远程备份，也未删除重复项目。
提交、标签、推送和清理均需按阶段另行批准；原始 `video_img` 完整保留。
