# 本机 CSV 因子库

因子定义和结果由客户端在使用者本机管理。初始化不连接服务器，
在线模式下的因子增删改也只操作本机。

## 初始化与 CSV

```bash
fe-cli init
```

默认创建 `~/fe-workspace`，包含 `factors.csv`、说明文件、`values/` 和 `runs/`。
也可执行 `fe-cli init DIRECTORY`，后续通过全局 `--workspace DIRECTORY`
使用该目录。终端 `/init [目录]` 可切换当前会话的工作目录。

CSV 初始仅有表头：

```csv
factor_id,name,expression,enabled,description
```

ID 唯一且稳定，名称与表达式不能为空，说明可留空；
`enabled` 接受 true/false 或 1/0。支持 UTF-8、BOM、标准 CSV 引号、
逗号和多行表达式。缺列、重复 ID、空必填项或损坏文件会报错并保留。
重复初始化保留已有文件和目录，不塞入演示因子或覆盖使用者内容。

## 维护定义

```bash
fe-cli factor add ret_mean20 --name "Mean return (20d)" \
  --expression 'ts_mean(col("ret"), 20)'
fe-cli factor list
fe-cli factor show ret_mean20
fe-cli factor update ret_mean20 --name "Mean daily return"
fe-cli factor delete ret_mean20
```

终端 `/factor` 选择定义，`/factor new` 和 `/factor edit ID` 打开内联编辑器。
表达式支持多行，Enter 保存、Ctrl+J 换行、Esc 取消；错误保留草稿。
也可直接编辑 CSV，保存时应关闭其他编辑器；客户端会检查已知并发修改，
但不能完全阻止不遵循文件锁的外部编辑器竞争。

保存仅校验字段和 CSV 结构，不执行表达式，不表示服务器语义、数据权限或数值验证通过。
可用表达式与字段应以既有服务提供的合同为准。程序自动生成定义指纹，不需手动维护版本列。

## 与任务和结果的关系

运行前重新读取启用的定义。在线运行要求存在且启用的库内因子；禁用或删除后不能继续提交。
任务编辑区中的单次修改不回写 CSV，已经冻结的请求不随后续定义修改而变化。
删除定义保留历史结果。

`values/` 保存校验后的不可变结果，`runs/` 保存请求和交付记录。
目录存在或因子定义保存成功不代表计算完成；只有最终本机提交回执发布后才算交付。
显式提交与校验流程见[单因子运行说明](single_run.md)。
`run all`、自动增量和数据最新日期规划尚未实现。
