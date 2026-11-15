# 单因子运行与本机结果

客户端从本机 CSV 选择启用的因子，冻结请求，经使用者明确授权后通过 SSH
交给既有服务器运行；随后按需下载并校验结果。客户端不安装计算引擎，
不提供服务器部署、计算执行或数据适配代码。

## 配置与提交

先向服务维护者取得获准使用的连接信息、解释器、数据目录、版本目录和清单摘要。
下列环境变量均须由使用者填写实际值，不是可直接使用的服务器配置：

```bash
fe-cli configure --host "$SSH_HOST" --user "$SSH_USER"
fe-cli configure-run \
  --remote-python "$REMOTE_FE_PYTHON" \
  --data-root "$DATA_ROOT" \
  --release-root "$RELEASE_ROOT" \
  --release-sha256 "$RELEASE_SHA256"
fe-cli prepare FACTOR_ID --start 2026-10-01 --end 2026-10-30
```

`configure`、`configure-run` 和 `prepare` 只操作本机，不连接服务器。
新运行配置必须同时提供上述四个运行参数；不猜测服务器路径或数据授权。
如果维护者确认同一版本支持多个节点，可显式使用 `--node shared`。
节点、资源和日期偏好见[资源选择](resource_selection.md)及根 [README](../README.md)。

确认要提交真实任务时执行：

```bash
fe-cli --online run FACTOR_ID --start 2026-10-01 --end 2026-10-30
fe-cli --online results JOB_ID
fe-cli runs
```

默认仍为模拟模式。在线终端中，`/run` 打开内联运行编辑区，按 Enter 确认才提交；
仅打开编辑区不提交。`results` / `collect` 与终端 `/results` 按需下载结果，
任务查询不会自动下载。提交不自动重试；在线取消和服务器安装由使用者另行处理。

新任务上传包只有冻结的 `request.json`，记录因子定义、日期、资源、源配置和服务版本身份。
运行代码由服务器提供，不随客户端请求上传。旧上传运行代码模式已移除；
旧配置和结果可读取，新提交必须先按[迁移说明](core_transition.md)更新配置。

## 结果校验与可见性

请求/结果协议为 `fe-cli-result-v1`。成功结果包含 `manifest.json` 和
`result.parquet`，清单须绑定冻结请求、请求摘要、任务编号及实际文件身份。
失败报告不能提交为成功结果。SSH 认证成功、任务完成、业务成功和本机交付成功分别记录。

客户端校验归档路径和成员类型、文件大小与摘要、请求和任务身份、实际 Parquet schema、
日期范围、键唯一性及有效性信息。结果列为 `ts: Date`、`inst: String`、
`value: Float64`、`is_valid: Boolean`、`is_deleted: Boolean`、
`invalid_reason: String`；无效行保留。来源声明与 PIT 信息按结果记录，
客户端校验不替代数据授权或历史可见性验收。

本机目录使用以下布局，任务及定义指纹均由程序生成：

```text
workspace/
  runtime.json
  runs/REQUEST_ID/
    request.json
    payload.tar.gz
    run.json
    manifest.json
    commit.json
  values/FACTOR_ID/DEFINITION_HASH/REQUEST_ID/result.parquet
```

目录存在不代表交付完成。只有校验后发布最终 `commit.json` 的结果才算本机可见提交。
重复导入相同结果幂等，身份或内容冲突会拒绝；删除因子定义不删除旧结果。
人工下载的归档可用 `fe-cli import-results ARCHIVE --request-id REQUEST_ID` 导入；
不指定请求编号时按包内冻结请求校验。

## 失败与恢复

状态记录准备、提交、执行失败或最终交付。提交后连接中断、回执缺失时保持
`SUBMISSION_UNKNOWN`，先查询任务和历史，再决定下一步，不自动重新提交。
下载或校验失败不会推进到本机完成状态；失败记录不生成成功结果回执。

结果若包含合法运行耗时则随回执保留；旧结果缺少计时时显示 unavailable，
不按收取时间推算。模拟耗时单独标记，不能作为真实计算性能证据。
本说明不代表任何具体服务器、数据源或节点已完成验收。
