# Linux 安装与 CLI 发布

## 当前状态

CLI 版本为 `0.1.0`；新增 `fe-cli` 命令，原 `fe` 入口保留。
`0.1.0` 已于 2026-11-15 发布：[GitHub Release](https://github.com/HKUST-QUANT-SOCIETY/fe-cli/releases/tag/v0.1.0)。
固定提交、工件摘要、安装验收和推广范围见[发布记录](releases/0.1.0.md)。
Core、服务器和数据接入不随客户端安装或升级，真实运行须另行配置和验收。

## 支持范围

- 首版安装器支持 Linux / WSL、x86_64、glibc >= 2.28；不支持 Alpine/musl、ARM、macOS 或 Windows 原生。
- 安装器用 uv 建立 Python 3.12 独立环境；已有 uv 可复用，否则从 Astral 的固定版本安装器临时获取。
  Python 缺失时由 uv 下载。不要求系统 Python，不执行 sudo，不修改系统包。
- 需要 `sh`、`curl`、`sha256sum` 和常见 GNU/Linux 文件命令，终端界面建议至少 80×24。
- 需要访问 GitHub、Astral/Python 下载源及 PyPI；首版不是离线包。
  依赖按 `uv.lock` 导出并校验 SHA256，只使用 wheel，不在用户机器编译 PyArrow。
- Python 包元数据允许 Python >=3.12；安装器的验证范围限定为上述 Python 3.12 平台。

## 安装和启动

免登录安装固定 `0.1.0` 并启动：

```bash
curl -fsSL 'https://github.com/HKUST-QUANT-SOCIETY/fe-cli/releases/download/v0.1.0/install.sh?download=1' | sh && export PATH="$HOME/.local/bin:$PATH"
fe-cli
```

执行上面命令会下载并运行安装脚本；也可以先下载审阅，再执行。
脚本 SHA256：`e027d1aa196fcaf3b946358262fb1359a8e6baba7dacd22f160fae0c550f30d4`。
也可以下载 Release 全部附件，从核对摘要后的本地目录安装：

```bash
sha256sum -c SHA256SUMS
FE_CLI_RELEASE_DIR="$PWD" sh ./install.sh
export PATH="$HOME/.local/bin:$PATH"
fe-cli --version
fe-cli
```

每份安装脚本固定版本和 wheel、依赖清单、发布清单摘要，不随 latest 变化安装另一版本。
摘要用于校验所下载内容与脚本一致；安装脚本本身的可信度依赖下载渠道及维护者公布的摘要。

默认安装入口为 `~/.local/bin/fe-cli`，版本环境在
`${XDG_DATA_HOME:-~/.local/share}/fe-cli/versions/`。
脚本不会修改 shell 启动文件。若 `~/.local/bin` 不在 PATH，将上面的 export 加入自己的
shell 配置后，新终端即可直接执行 `fe-cli`；当前 shell 的 PATH 需要显式 export。
可通过绝对路径 `FE_CLI_BIN_DIR` 和 `FE_CLI_INSTALL_ROOT` 自定义安装位置。

不需要安装器时，也可使用项目 uv 工具流程安装已校验的 wheel：

```bash
uv tool install --python 3.12 --constraints requirements.txt ./fe_cli-0.1.0-py3-none-any.whl
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

## 升级、旧实验版与回退

运行目标版本的安装脚本即可切换入口；新环境完成安装并检查版本后才替换入口。
旧版本环境、workspace、因子 CSV、配置和历史结果均保留。同一工件重复安装复用已完成环境。
安装失败不会切换现有入口；未完成环境保留并报错，可指定新的 `FE_CLI_INSTALL_ROOT`，
不自动删除或重建既有环境。异常中断遗留的 `.install-lock` 需要先确认没有安装进程，再人工处理。

首次从旧实验 `0.5.4` 切换到受管理 `0.1.0` 时，需明确选择 `0.1.0` 工件，
版本排序不能代表迁移。若此前使用 `uv tool install`，先用 `uv tool uninstall fe-cli`
移除旧工具入口，再使用新安装器；这不删除 workspace。安装器不会覆盖其他来源的同名命令。

回退时运行先前保存且已校验的安装脚本，或直接执行保留版本环境中的 `bin/fe-cli`。
回退客户端不会回退服务器或迁移后的数据；本次安装不改变配置/schema 和数值语义。
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
构建不创建标签、不上传、不修改仓库可见性。只安装候选 wheel 并验证用户主路径，
通过后推广相同摘要的工件，不在推广时重建。

公开前核对下载仓库的公开范围，包括完整历史及旧服务端适配代码；本地模式密钥扫描不能
替代完整审计。GitHub 写入由维护者通过本机 gh 执行，每次写前确认账号为 Polaris-YC。
标签须指向清单 source_commit；不得覆盖既有版本、标签或 Release 附件。
正式发布需同步实际下载地址、版本日志、验收范围和独立推广记录。

参考：[uv 安装器选项](https://docs.astral.sh/uv/reference/installer/)、
[项目维护规范](shared_release_workflow.md)。
