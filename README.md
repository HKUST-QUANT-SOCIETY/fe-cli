# fe-cli

协会 Factor Engine 研究终端。Python + Textual 客户端，提供常驻命令输入框、
可筛选 `/` 菜单、参数确认、按需任务查询与结果交付。界面使用近黑底色与柔和的状态色，
任务状态通过 `/jobs` 输出，不常驻占用主界面。

本仓库仅包含客户端，不包含 Factor Engine 后端、服务器部署器或数据准备实现。
支持本地模拟、通过 SSH 认证的 QCL 查询和调用既有服务器入口执行单因子任务。默认启动模拟模式；显式使用
`--online run`，或在在线终端的运行编辑区按 Enter 确认，才提交真实任务。
`results` / `collect` 下载并校验结果后写入本机；在线取消与服务器环境安装不支持。
默认任务请求为 `quant-a / 6 CPU / 18G`，可通过 `/node` 和 `/resources` 选择；
模拟报告的 `PASS` 只表示演示流程完成。

## 近期迭代

- 2026-11-16：补充 [Core 与 CLI 调用范例](docs/core_call_examples.md)，明确 Core 支持裸字段，
  当前客户端提交仍要求已知字段使用 `col()`；提供可完整运行的合成计算与落湖示例。
- 2026-11-15：已发布客户端完成 A 节点远程隔离演练，合成成功/失败、幂等取回与篡改拒绝通过；
  尚不代表 Core 或真实数据验收。见[演练范围与结果](docs/remote_rehearsal.md)。
- 2026-11-15：发布 `0.1.1`，增加后台更新提示、`--check-update` 和 `--update`；
  通过 `0.1.0` 升级及回退、匿名安装验收。见[客户端更新](#客户端更新)与[发布记录](docs/releases/0.1.1.md)。
- 2026-11-15：发布 `0.1.0`，支持 Linux curl 安装并直接用 `fe-cli` 启动；
  安装保留旧环境与 workspace。见[安装手册](docs/installation.md)及[发布记录](docs/releases/0.1.0.md)。
- 2026-11-15：公开版收敛为纯客户端，只上传请求并调用固定服务器入口；旧代码上传模式停用，
  已有结果仍可读取。见[迁移说明](docs/core_transition.md)和[开发记录](docs/development.md)。

## 启动

支持 Linux / WSL x86_64、glibc >=2.28。安装器自动准备 uv、Python 3.12 和依赖，
不需要 sudo 或克隆源码：

```bash
curl -fsSL 'https://github.com/HKUST-QUANT-SOCIETY/fe-cli/releases/download/v0.1.1/install.sh?download=1' | sh && export PATH="$HOME/.local/bin:$PATH"
fe-cli
```

输入 `/` 打开菜单，使用上下键选择、Tab 补全、Enter 执行。
选中命令后，输入框内以灰字显示参数用法，例如 `/factor [list | new | show ID | edit ID | delete ID | use ID]`。
Tab 只补全命令名称；参数提示不属于实际输入，Enter 不会自动执行灰字内容。
`Ctrl+N` 打开当前模式的任务确认，`Ctrl+L` 返回命令框，`Ctrl+C` 退出；Esc 关闭菜单或返回。
运行和因子编辑区内 Enter 确认、Ctrl+J 换行（支持时也可用 Shift+Enter）、Esc 取消；
节点/资源下拉框及 Details 标题的 Enter 仍用于选择或展开。
F2 在原会话区域切换只读选择模式：鼠标拖选或 Shift+方向键选择，Ctrl+A 全选，
Ctrl+C / F8 发送复制请求，Esc 或 F2 返回。选择期间固定当前文字，新输出在返回后可见；
长路径复制保留完整内容。复制需要宿主终端允许 OSC 52 剪贴板访问，界面无法确认系统剪贴板是否更新。
支持 80×24 及以上终端，较窄窗口也可使用，任务输出随宽度换行。
本阶段界面统一使用英文；颜色遵循终端和 `NO_COLOR` 设置。
新输出出现或会话区尺寸变化时，会自动滚动到底部，保持最新内容可见。
顶部只显示模式与查询状态；下一任务的资源摘要位于输入框下方。
短会话贴近输入区显示；命令结果通过留白、文字层级和少量薄荷绿、淡蓝、杏色区分。

默认安装到用户目录；需要在新终端继续使用时，请确保 `~/.local/bin` 已加入 shell 的 PATH。
支持范围、固定工件安装、升级和回退见[安装手册](docs/installation.md)。

## 客户端更新

以下功能从 `0.1.1` 起可用；`0.1.0` 用户先运行上方安装命令升级一次。

```bash
fe-cli --check-update   # 主动检查最新正式客户端
fe-cli --update         # 确认安装最新正式客户端
fe-cli --no-update-check
```

交互启动后会在后台查询公开 GitHub Release，有新版本时提示当前版本、目标版本和升级指令。
成功检查缓存 24 小时，失败检查间隔至少 1 小时；断网和检查失败不影响界面。
普通子命令、`--version` 和 `--help` 不触发检查，不改动脚本的 JSON 输出。
可通过 `--no-update-check` 或 `FE_CLI_NO_UPDATE_CHECK=1` 关闭后台检查，主动检查仍可使用。

`--update` 仅支持新版官方安装器管理的当前安装。它重新查询正式版本、核对安装脚本摘要，
安装完成后切换入口，保留旧环境和 workspace；重新运行 `fe-cli` 才使用新版。
版本检查不自动安装更新；客户端升级不切换服务器 Core。迁移和其他安装方式见[安装手册](docs/installation.md#client-updates)。

## 初始化本机因子库

```bash
fe-cli init
```

默认创建 `~/fe-workspace/`，并打印 CSV 和说明文件的完整路径：

```text
fe-workspace/
  factors.csv
  README.md
  values/
  runs/
```

`factors.csv` 初始只有表头，不自动塞入演示因子。可以直接编辑，或通过 CLI 维护。
重复 init 保留已有 CSV 和说明文件；损坏文件会报错，不会静默重建。
`values/` 保存校验后的不可变结果，`runs/` 保存冻结请求、提交记录、manifest 和最终提交回执。
例如 `values/FACTOR_ID/DEFINITION_HASH/REQUEST_ID/result.parquet`；只有最终本机
`commit.json` 已发布的结果算交付完成。增量计算尚未实现。

自定义位置使用 `fe-cli init /path/to/workspace`；之后在命令前指定
`--workspace /path/to/workspace`。终端里也可使用 `/init [目录]`，仅切换当前会话的工作目录。

CSV 的固定列为：

```csv
factor_id,name,expression,enabled,description
```

ID 唯一且稳定，名称与表达式不能为空；`enabled` 使用 true/false 或 1/0，说明可以留空。
保存为 UTF-8，支持 BOM、标准 CSV 引号、带逗号的表达式和多行表达式。
物理空行会忽略，未正确加引号的逗号、缺列或重复 ID 会报错。保存前关闭其他编辑器，
CLI 会检查并发修改并协调自己的写入，但无法完全阻止不遵循锁的外部编辑器竞争。

```bash
fe-cli factor add ret_mean20 --name "Mean return (20d)" \
  --expression 'ts_mean(col("ret"), 20)'
fe-cli factor list
fe-cli factor show ret_mean20
fe-cli factor update ret_mean20 --name "Mean daily return"
fe-cli factor delete ret_mean20
```

保存只验证定义字段和 CSV 结构，不执行 Python，也不表示 FE DSL 已校验。
删除定义保留本机结果文件。修改表达式会改变自动生成的定义指纹，无须手工维护版本字段。

在交互终端输入 `/factor` 可选择已保存的因子；`/factor new` 在命令框上方展开内联编辑器，
`/factor edit ID` 修改定义，`/factor delete ID` 删除定义。表达式支持多行输入；
保存冲突时保留编辑器中的内容，便于修改或复制。普通 CLI 使用 `factor update` 更新定义。
新建和编辑均嵌在会话内，不打开独立窗口：ID 与名称并排，多行表达式为主要编辑区，
描述和启用状态收进 Details。Enter 或 Ctrl+S 保存、Esc 取消，结束后返回命令框。
编辑器底部只有一行提示；表达式和描述中用 Ctrl+J 换行，Enter 保存。
编辑期间需先保存或取消再打开其他编辑流程；任务查询与帮助仍可使用。
这些操作在 `--online` 模式下也只读写本机文件，不调用 SSH。

模拟模式下 `fe-cli run ret_mean20` 会读取库中表达式；也可先在终端选择因子，再 `/run`，
表单会预填最新定义。本次运行中的修改不会改写 CSV。禁用或不存在的库内因子不能运行。
在线运行要求选择启用的库内因子，再明确提交；`run all`、真实增量和数据源最新日期规划仍待后续实现。
磁盘源字段使用 `col("ret")`、`col("pb_ratio")` 等明确写法；已知裸字段在提交前报错，
不会自动把目录字段解释成磁盘列，也不修改 CSV 表达式。
这是当前客户端的提交限制；直接使用新版 Core 的 `parse_factor` 支持裸字段和 `col()`，
详见 [调用范例与入口排查](docs/core_call_examples.md)。

## 在线服务

先在本机保存连接参数；此操作不会连接服务器，也不会读取私钥内容：

```bash
fe-cli configure --host YOUR_QCL_HOST --user YOUR_QCL
fe-cli --online
```

如果已有本机 SSH 别名，也可只指定 `--host 你的别名`，沿用 SSH 配置中的账号和密钥。
非默认私钥增加 `--identity-file /path/to/key`；使用 Windows OpenSSH 时可指定
`--ssh-executable /mnt/c/Windows/System32/OpenSSH/ssh.exe`，并给出 Windows 格式的密钥路径。
不会复制或读取私钥，不保存密码。加密密钥需要提前在相应的 SSH agent 中解锁。
主机必须已在对应 OpenSSH 的 known_hosts 中可信登记；主机校验失败会报错，不会自动绕过。

配置默认保存在 `${XDG_CONFIG_HOME:-~/.config}/fe-cli/connection.json`，文件权限为 0600。
使用全局 `--config PATH` 可以隔离配置；损坏配置先备份并修复，程序不会静默覆盖。
`configure` 只做本地配置，即使指定 `--demo` 也不会连接。

| 在线命令 | 远程入口 |
| --- | --- |
| `/setup` | 独立 SSH 握手，诊断连接、公钥认证与实际认证密钥指纹；不执行远程命令 |
| `/envs` | `cluster-envs`：列出注册环境 |
| `/jobs` | `cluster-status`：当前任务 |
| `/history` | `cluster-history`：保留表头，按任务编号倒序显示最近 10 条记录 |
| `/history full` | 同一只读入口，显示服务器返回的完整历史 |
| `/run [因子ID]` | 内联编辑参数，按 Enter 确认提交一个真实任务 |
| `/results [任务ID或请求ID]`、`/collect 任务ID` | 下载、校验并提交真实结果到本机 |
| `/runs` | 查看本机单因子请求和交付状态，不连接服务器 |
| `/node [A\|C]`、`/resources [CPU数]` | 本机设置下一任务的节点和资源，不调用 SSH |
| `/range [起始日期 结束日期]` | 本机设置未来任务的默认日期范围，不调用 SSH |
| `/help`、`/quit` | 本机帮助、退出 |

查询异步执行，等待时仍可输入命令、查看帮助或退出。一次只运行一个查询。
服务器输出保留原有文本格式，单次输出总量上限 1 MiB；不猜测任务表字段或业务报告状态。
默认连接超时 10 秒，查询超时 30 秒，可在 configure 中调整 `--connect-timeout`、`--timeout`。
超时或连接错误不自动重试。SSH 查询成功不代表 FE 安装、COS 或真实数据权限已验证。

`/setup` 不打开远程 shell，不申请算力，也不读取环境列表或用户组。
只有 OpenSSH 明确确认公钥认证成功才显示认证指纹；仅出现候选密钥或服务器接受密钥的提示，
不算认证成功。网络、主机校验与认证失败分别说明，证据不足时保持 unknown。
诊断只返回目标、账号、连接/认证状态和公钥指纹等摘要，不输出完整调试日志或读取私钥正文。

也可直接查询并得到带 `demo: false` 的 JSON：

```bash
fe-cli --online setup
fe-cli --online jobs
fe-cli --online history
fe-cli --online history full
```

普通 CLI `setup` 返回 `diagnostic` 对象；认证成功退出 0，失败或未确认退出 1。

在线模式下 `run` 提交一个真实任务，`results` / `collect` 下载并校验后提交到本机；
对应终端入口为 `/run` 和 `/results`。`cancel` 仍不支持在线取消，需用户使用集群工具处理。
`setup`、`envs`、`history [full]` 和 `collect` 需要 `--online`。
历史的 10 条限制只统计记录，不包含表头与分隔线；完整模式保持服务器返回顺序。
`history full` 不会扩大服务器自身的历史覆盖范围，现有 1 MiB 查询输出上限仍然生效。

## 单因子真实运行与本机结果

先向服务维护者取得既有服务器入口、解释器、数据根目录和版本清单摘要；以下变量须由使用者
设置为实际获准值。配置命令只写本机 `runtime.json`，不安装后端或连接服务器：

```bash
fe-cli configure-run --remote-python "$REMOTE_FE_PYTHON" \
  --data-root "$REMOTE_DATA_ROOT" --release-root "$REMOTE_RELEASE_ROOT" \
  --release-sha256 "$REMOTE_RELEASE_SHA256"
fe-cli prepare ret_mean20 --start 2026-10-01 --end 2026-10-30
# 以下命令会真实提交；仅在决定运行时执行
fe-cli --online run ret_mean20 --start 2026-10-01 --end 2026-10-30
fe-cli --online results JOB_ID
# results 与 collect 使用同一下载、校验和本机提交流程
fe-cli --online collect JOB_ID
fe-cli runs
```

运行配置默认 A、`research`、30 分钟，默认资源为 6 CPU / 18G。
解释器、数据根目录、服务器入口和摘要必须显式配置；不猜测任何现有服务器路径。
配置和 `prepare` 不连接服务器；`prepare` 冻结因子定义与日期，运行包只包含请求 JSON。
在线终端中选择因子，再 `/run`，在命令框上方核对参数后按 Enter 提交；仅展开编辑区不会提交。
完成后 `/results JOB_ID` 才取回真实 Parquet 并显示已提交的本机路径；`/runs` 查看本机交付状态。

也可将人工下载的归档交给 `fe-cli import-results /path/to/results.tar.gz`，无需连接服务器。
源码提供运行与交付链路，真实服务器安装、数据权限和首个真实任务仍须以实际验收证据确认。
源字段、预热、协议、目录与状态边界见[单因子运行说明](docs/single_run.md)。

## 默认日期范围

在交互终端输入 `/range`，会在命令框上方显示当前起止日期，Enter 保存、Esc 取消。
也可直接设置，例如：

```text
/range 2020-01-01 2025-12-31
```

普通命令行使用 `fe-cli range` 查看默认值，
`fe-cli range 2020-01-01 2025-12-31` 保存；即使指定 `--online`，该设置也只操作本机。
两种模式及各工作目录共用 `${XDG_CONFIG_HOME:-~/.config}/fe-cli/range.json`，重启后保留。
全局参数 `--range-config PATH` 可指定独立配置文件。

新打开的 `/run` 和 CLI `run` / `prepare` 继承已保存的范围；运行表单和 `--start` / `--end`
仍可覆盖本次任务，未指定的一端继续继承默认值。单次覆盖不回写全局设置，已有任务不变。
首次使用且无配置时仍为 `2026-10-01` 至 `2026-10-31`；日期须为真实的 YYYY-MM-DD，
且起始日期不晚于结束日期。损坏配置会报错并保留，不会静默重建。

## 节点与任务资源

在两种模式下都可以输入以下命令；不带参数时，选项直接在命令框上方展开：

```text
/node C
/resources 8
```

这将下一任务的默认请求设为 C、8 CPU / 24 GB。默认初始值为 A、6 CPU / 18 GB。
每档固定增加 **1 CPU + 3 GB**，内存随 CPU 档位变化。
选择时输入框与会话保持可见，↑↓ 切换、Enter 应用、Esc 取消；开始输入新命令也会收起选择器。
按集群手册，A 提供 1–6 档（最高 6 CPU / 18 GB），C 提供 1–8 档（最高 8 CPU / 24 GB）。
请求不等于立即分配；手册注明奇数 CPU 可能按偶数槽位占用，实际调度以服务器规则为准。

设置保存在 `${XDG_CONFIG_HOME:-~/.config}/fe-cli/resources.json`，重新启动继续使用。
`--resource-config PATH` 可以隔离资源偏好，它与 SSH `--config` 分开。
在线模式下这些命令只保存本机选择，不申请算力，也不改变查询范围或已有任务。

`/run` 的内联编辑区会预填当前选择，展开 Details 可为这一任务单独覆盖节点和资源；
创建时将请求写入任务记录。后续修改默认值不会改变已有任务，单次覆盖也不改写默认值。
从 C 的高档位切换到 A，会显示调整后的有效档位，再由用户确认。
直接输入 `/node A` 时，如果当前档位超过 A 上限，会拒绝保存；先 `/resources 6` 即可切换。

普通命令行同样支持本机偏好和单次覆盖：

```bash
fe-cli node C
fe-cli resources 8
fe-cli node                  # 查看当前选择
fe-cli resources             # 查看当前 CPU/内存请求
fe-cli run --node A --cpus 2  # 仅此模拟任务使用 A、2 CPU / 6 GB
```

## 模拟交互命令

| 命令 | 行为 |
| --- | --- |
| `/run` | 在命令框上方编辑名称、公式与日期，按 Enter 创建模拟任务 |
| `/jobs` | 在输出区显示任务状态快照；再次调用刷新，用 `/results 编号` 查看报告 |
| `/node [A\|C]` | 选择下一任务的默认节点，不带参数在输入框上方展开选项 |
| `/resources [CPU数]` | 按 1 CPU / 3 GB 档位选择资源，不带参数在输入框上方展开选项 |
| `/range [起始日期 结束日期]` | 保存未来运行的默认日期；不带参数时在命令框上方编辑 |
| `/results [编号]` | 查看指定或最新任务的合成报告摘要；增加 `--raw` 展开完整 JSON |
| `/cancel 编号` | 取消尚未结束的模拟任务 |
| `/init [目录]` | 创建本机工作目录和 CSV，保留已有文件 |
| `/factor` | 选择已保存的因子；new/edit/delete 管理本机定义 |
| `/data` | 查看示例字段与口径；不是实际可用数据目录 |
| `/setup` | 查看本地模式与资源约定；不执行 SSH 检查 |
| `/help`、`/quit` | 帮助、退出 |

创建后约 2 秒进入模拟运行，约 5 秒结束。状态按墙钟时间推进；
关闭客户端后重新打开，也能看到任务进展。没有后台计算进程。
公式只作参数保存，不解析或验证 FE 语义。报告行数是固定规则生成的合成值，
历史 PIT 保持 `UNKNOWN`，不能用于研究结论。

## 普通命令行

可用于脚本或不支持全屏界面的环境；以下模拟命令输出 JSON，并带 `demo: true`。

```bash
fe-cli --state-dir .demo-state run --factor momentum_20d \
  --formula 'ts_mean(ret, 20)' --start 2026-10-01 --end 2026-10-31
fe-cli --state-dir .demo-state jobs
# 约 5 秒后；编号以 run 返回值为准
fe-cli --state-dir .demo-state results demo-0001
```

`run --fail` 可演示失败状态；`cancel 编号` 取消任务。
全局参数 `--state-dir`、`--demo` 应放在子命令前面。
参数或状态错误返回非零退出码，不会静默覆盖损坏文件。

## 本地状态

默认保存到 `${XDG_STATE_HOME:-~/.local/state}/fe-cli/demo-state.json`。
使用 `--state-dir` 可隔离不同演示。该目录只保存模拟参数和状态，不保存密钥。
多个客户端共享同一目录时，文件锁和原子替换保护写入；忙碌时提示稍后重试。
如果状态损坏，先备份并移走报错中指定的文件，再重新启动。

## 开发

源码开发需要 Python >=3.12 和 [uv](https://docs.astral.sh/uv/)；在本项目目录执行：

```bash
uv sync --locked
uv run fe-cli --demo
uv run ruff check .
uv run pytest -q tests/test_app.py   # 开发时做定点验证
uv run pytest -q                     # 收尾全量一次
uv build
```

- `src/fe_cli/app.py`、`screens.py`、`terminal.tcss`：终端界面。
- `src/fe_cli/backend.py`：本地模拟生命周期与状态存储。
- `src/fe_cli/cli.py`：普通命令行及界面入口。
- `src/fe_cli/config.py`、`remote.py`：本地连接配置和只读 SSH 查询。
- `execution.py`、`job_transport.py`：固定请求、SSH 授权入口调用和结果下载。
- `delivery.py`、`run_contract.py`：真实 Parquet 校验、不可变结果与本机提交协议。
- `src/fe_cli/resources.py`：节点、资源档位校验和本地偏好。
- [开发记录](docs/development.md)：范围、证据与后续阶段。
- [节点与资源选择记录](docs/resource_selection.md)：0.3.0 行为及定点验证。
- [本机因子库记录](docs/local_catalog.md)：0.4.0 初始化、CSV 与定义管理。
- [文档索引](docs/README.md)：工程规则、历史记录与后续任务。

本机客户端不安装整个 FE；运行端复用使用者指定的服务器环境。
提交只尝试一次；确认丢失时保持 `SUBMISSION_UNKNOWN`，先查集群历史，不自动重复提交。
开发会话不实际提交作业或安装服务器环境；真实提交由使用者显式操作应用触发。
