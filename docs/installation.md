# CLI 安装与发布

## 当前状态

最新正式版本为 `0.3.0`，Linux 与 macOS 共用版本，`fe` 兼容入口保留：
[GitHub Release](https://github.com/HKUST-QUANT-SOCIETY/fe-cli/releases/tag/v0.3.0)。
固定提交、工件摘要、原生验收和发布范围见[发布记录](releases/0.3.0.md)，
历史版本见 [0.2.0 记录](releases/0.2.0.md)、[0.1.1 记录](releases/0.1.1.md)、[0.1.0 记录](releases/0.1.0.md)。
Core、服务器和数据接入不随客户端安装或升级，真实运行须另行配置和验收。

<a id="release-policy"></a>

## 发布与维护范围

CLI 暂不区分发布渠道，仅维护最新正式版；不建立旧版本维护分支或为旧版回补修复。
官方 curl 安装入口固定为 `releases/latest/download/install.sh`，只选择下载时的最新正式版，
不提供版本号或渠道参数。新版本发布后入口自动指向该版本，用户无须修改安装命令。
发布时须将完整验收的新版本标记为 GitHub 的 latest；旧版本不得重新标记为 latest。

历史 Release、标签、附件及发布记录保留供下载、追溯和必要的维护者排查，不覆盖或删除；
保留可下载状态不代表旧版仍受维护。未发布代码及本地候选包仅用于开发验收，不构成独立用户渠道。
每份实际安装脚本仍固定自身版本及工件摘要；latest 仅负责选择脚本，一次安装不会混用两次发布的文件。
本政策不改变 Core 独立编号、服务器固定发布版本部署或既有 workspace 与结果保留规则。

## 支持范围

- Linux / WSL 支持 x86_64、glibc >=2.28；macOS 支持 14+、Apple Silicon arm64 或 Intel x86_64。
  不支持 Alpine/musl、Linux ARM 或 Windows 原生。macOS 原生 CI 已覆盖 15 两架构，最低 14 尚未实机验证。
- 安装器用 uv 建立 Python 3.12 独立环境；已有 uv 可复用，否则从 Astral 的固定版本安装器临时获取。
  Python 缺失时由 uv 下载。不要求系统 Python，不执行 sudo，不修改系统包。
- Linux 需要 `sh`、`curl`、`sha256sum` 及常见 GNU/Linux 命令；macOS 使用系统 `shasum`、`sw_vers`
  及 BSD 文件命令，不要求 GNU coreutils。终端界面建议至少 80×24。
- 需要访问 GitHub、Astral/Python 下载源及 PyPI；不是离线包。
  依赖按 `uv.lock` 导出并校验 SHA256，只使用 wheel，不在用户机器编译 PyArrow。
- Python 包元数据允许 Python >=3.12；安装器的验证范围限定为上述 Python 3.12 平台。

<a id="macos-preview"></a>

## macOS 安装与原生验收

从正式 `0.3.0` 起，macOS 与 Linux 共享同一个客户端版本、通用 wheel、依赖锁及 `install.sh`。
直接使用下方最新版 curl 命令，不需克隆源码或选择平台版本；也可通过 `fe-cli --update` 更新官方安装器管理的安装。
历史 `0.2.0` 正式附件只支持 Linux；此前带 0.2.0 开发元数据的本地 Mac 试用包不属于正式发布。
这类试用安装可重新执行最新版 curl 命令迁移，保留既有环境和 workspace。

原生 CI 已在 macOS 15 的 arm64 / x86_64 上完成全量测试、同一组正式工件安装、
源码目录外主路径和重复安装的数据保留检查，详见[0.3.0 记录](releases/0.3.0.md)。
安装包不包含 Python 或依赖 wheel；仍需网络获取缺失依赖。

安装器使用系统 `sh`、`curl`、`shasum -a 256`、`sw_vers` 和 BSD 文件命令，无需 GNU coreutils、
Homebrew 或管理员权限；缺少 uv 时自动获取固定版本，Python 及依赖由 uv 准备，只安装 wheel。
Apple Silicon 应从原生 arm64 终端运行；Rosetta x86_64 会按 Intel 进程架构安装，本轮不承诺跨架构迁移。
默认命令仍为 `~/.local/bin/fe-cli`，环境仍在 `${XDG_DATA_HOME:-~/.local/share}/fe-cli/versions/`；
配置、缓存及 `~/fe-workspace` 沿用既有位置，不迁移到 `~/Library`。
macOS 默认 zsh 可将 `export PATH="$HOME/.local/bin:$PATH"` 加入 `~/.zshrc`，安装器不修改启动文件。

SSH 默认使用本机 `ssh`，账号、密钥及 known_hosts 按该 OpenSSH 的配置管理，不复制私钥。
界面建议至少 80×24；复制仍取决于终端是否支持并允许 OSC 52，未验证的终端可使用其自身选择复制。
最低 macOS 14、自动下载 uv 的 Mac 首装、真实终端的按键/鼠标/中文/剪贴板和真实 SSH/任务
尚未独立验收；CI 的虚拟终端测试不能代替这些范围。Core 与节点兼容声明不随客户端发布扩大。

## 安装和启动

免登录安装最新正式版并启动（本次核对为 `0.3.0`）：

```bash
curl -fsSL 'https://github.com/HKUST-QUANT-SOCIETY/fe-cli/releases/latest/download/install.sh' | sh && export PATH="$HOME/.local/bin:$PATH"
fe-cli
```

执行上面命令会下载并运行安装脚本；也可以先下载审阅，再执行。
本次下载到的 `0.3.0` 脚本 SHA256：`9acd1b14ed912568eb5063896b2ba6da4541590a3229668116bbf5b723623b94`。
该摘要仅对应这一固定工件，后续最新版本的摘要以对应发布记录为准。
维护者也可下载最新 Release 全部附件，从核对摘要后的本地目录验收安装：
以下校验命令用于 Linux；macOS 将首行替换为 `shasum -a 256 -c SHA256SUMS`。

```bash
sha256sum -c SHA256SUMS
FE_CLI_RELEASE_DIR="$PWD" sh ./install.sh
export PATH="$HOME/.local/bin:$PATH"
fe-cli --version
fe-cli
```

每份安装脚本固定版本和 wheel、依赖清单、发布清单摘要；脚本下载后不随 latest 变化安装另一版本。
摘要用于校验所下载内容与脚本一致；安装脚本本身的可信度依赖下载渠道及维护者公布的摘要。

默认安装入口为 `~/.local/bin/fe-cli`，版本环境在
`${XDG_DATA_HOME:-~/.local/share}/fe-cli/versions/`。
脚本不会修改 shell 启动文件。若 `~/.local/bin` 不在 PATH，将上面的 export 加入自己的
shell 配置后，新终端即可直接执行 `fe-cli`；当前 shell 的 PATH 需要显式 export。
可通过绝对路径 `FE_CLI_BIN_DIR` 和 `FE_CLI_INSTALL_ROOT` 自定义安装位置。

维护者不使用安装器验收工件时，也可用项目 uv 工具流程安装已校验的 wheel（以下以 `0.3.0` 为例）：

```bash
uv tool install --python 3.12 --constraints requirements.txt ./fe_cli-0.3.0-py3-none-any.whl
fe-cli --version
fe-cli
```

此工具安装流程和 curl 安装器分别管理环境；二选一，已有同名入口时显式处理冲突。
`fe` 是 wheel 内的兼容入口；curl 安装器只向用户 bin 目录发布 `fe-cli`，避免占用原有 `fe`。

## 首次使用

```bash
fe-cli init
fe-cli factor add ret_mean20 --name "Mean return (20d)" --expression 'ts_mean(col("ret"), 20)'
fe-cli factor list
fe-cli --demo
```

默认使用 `~/fe-workspace`，仅创建空因子库及必要目录。模拟模式不访问服务器。
SSH、服务器运行环境、节点/资源/日期、真实 run/results 操作见根 [README](../README.md#在线服务)。
首次配置使用实际 QCL 主机和账号；不得将模拟通过当成真实 Core 或数据权限通过。
当前请求/结果协议为 `fe-cli-result-v1`，公共运行清单协议为 `fe-cli-public-release-v1`。
本次安装验证不扩大任何 Core/节点兼容声明。

<a id="client-updates"></a>

## 客户端更新（0.1.1）

`0.1.1` 增加 `fe-cli --check-update` 和 `fe-cli --update`，源码环境也可用
`uv run fe-cli --check-update` 检查。历史 `0.1.0` 不支持这两个参数，需先运行上方安装脚本。
版本号来自包元数据，数字分段比较；只更新到更高的正式版，不自动降级或选取预发布版。

交互启动后在后台检查固定仓库的公开 Release；请求只携带客户端标识，
不发送 workspace、SSH 配置或因子内容。成功结果在 XDG 缓存目录下保存 24 小时，
失败结果节流 1 小时。缓存损坏或不可写不影响使用；后台联网错误静默。
`--no-update-check` 或 `FE_CLI_NO_UPDATE_CHECK=1` 关闭后台检查。
普通命令和帮助/版本不查询更新；显式 `--check-update` / `--update` 始终重新查询，
失败返回非零退出码并说明原因，不从旧缓存安装。

`--update` 需要当前环境的安装归属记录和当前选中的官方命令入口。
它先核对 GitHub 发布元数据提供的 SHA256，再执行固定版本安装器；
安装器同时核对目标版本及工件摘要，保留旧环境。完成后退出，重新运行命令才启动新版。
下载有容量限制和超时，HTTPS 重定向不能降级为 HTTP；仅通过固定官方仓库发现版本。
本功能依据 GitHub 发布渠道身份校验，并不提供独立签名密钥体系。

旧 `0.1.0` 既没有更新命令，也没有新的安装归属记录：
先运行官方最新版 curl 安装命令完成一次迁移，之后即可使用 `--update`。
`uv tool`、pip 或源码安装应沿用原安装方式升级；自更新不会覆盖它们的环境或入口。
自定义安装路径由安装器记录，升级继续使用相同位置。

## 升级与历史工件

重新运行官方 curl 安装命令或显式执行 `fe-cli --update` 升级到最新正式版；
新环境完成安装并检查版本后才替换入口。
旧版本环境、workspace、因子 CSV、配置和历史结果均保留。同一工件重复安装复用已完成环境。
安装失败不会切换现有入口；未完成环境保留并报错，可指定新的 `FE_CLI_INSTALL_ROOT`，
不自动删除或重建既有环境。异常中断遗留的 `.install-lock` 需要先确认没有安装进程，再人工处理。

旧实验 `0.5.4` 的版本排序高于受管理正式版，迁移应显式使用当前最新版 curl 入口，
不能依赖普通升级排序判断。若此前使用 `uv tool install`，先显式处理原工具入口，
再使用官方安装器；安装器不会覆盖其他来源的同名命令，也不删除 workspace。

历史附件和本机已保留的环境仅用于追溯或维护者必要的故障恢复，不提供面向用户的历史版本选择或维护承诺。
维护者恢复旧客户端不会回退服务器或迁移后的数据，不保证旧版兼容当前服务器。
卸载时只处理确认归属的命令入口与安装目录；不删除 workspace、XDG 配置或历史结果。

## 维护者构建与推广

先完成源码检查点提交，再从固定提交构建到新的输出目录：

```bash
uv run scripts/build_cli_release.py --ref HEAD \
  --repository HKUST-QUANT-SOCIETY/fe-cli --output dist/cli-candidate-NEW
```

构建器用 Git archive 隔离未提交、忽略和本机文件；执行 uv build 和锁定依赖导出，
生成 wheel、sdist、requirements.txt、release.json、install.sh、SHA256SUMS。
清单记录版本、源提交、下载仓库、依赖及工件摘要、平台和协议；已有输出目录拒绝覆盖。
`compatibility.installer` 保留原 Linux 字段以兼容既有读取；新增 `installer_targets` 列举 Linux
及两个 macOS 目标，Mac 的 `pending_native_validation` 明确表示尚未完成原生验收。
构建不创建标签、不上传、不修改仓库可见性。只安装候选 wheel 并验证用户主路径，
通过后推广相同摘要的工件，不在推广时重建。

公开前核对下载仓库的公开范围，包括完整历史及旧服务端适配代码；本地模式密钥扫描不能
替代完整审计。GitHub 写入由维护者通过本机 gh 执行，每次写前确认账号为 Polaris-YC。
标签须指向清单 source_commit；不得覆盖既有版本、标签或 Release 附件。
正式发布前先完成全部附件及验收，再发布并设置为 latest；核对官方 curl 入口与 `--update`
指向同一正式版本。正式发布需同步实际下载地址、版本日志、验收范围和发布记录；历史版本只归档、不回补修复。

参考：[GitHub 发布 API](https://docs.github.com/en/rest/releases/releases#get-the-latest-release)、
[GitHub 最新版附件链接](https://docs.github.com/en/repositories/releasing-projects-on-github/linking-to-releases)、
[uv 安装器选项](https://docs.astral.sh/uv/reference/installer/)、
[项目维护规范](shared_release_workflow.md)。
