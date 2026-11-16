# FE 调用范例：裸字段与 col

核验日期：2026-11-16。本文补充根 README 的公式与运行说明，区分直接调用 Core 和通过 fe-cli 提交。

## 先确认调用入口

| 入口 | `ts_mean(close,20)` | `ts_mean(col("close"),20)` |
| --- | --- | --- |
| Core `parse_factor` → `FactorEngine`，默认 daily/native | 支持 | 支持 |
| Core YAML 的 `factor.expr` | 支持 | 支持 |
| fe-cli 0.2.0 的 `prepare` / 在线提交 | 支持，原样提交 | 支持，原样提交 |
| 已发布 fe-cli 0.1.1 的 `prepare` / 在线提交 | 已知裸字段在客户端被拒绝 | 使用此写法 |
| fe-cli 保存 CSV / 模拟运行 | 可以保存或演示，不证明能真实计算 | 同样不代表真实计算验收 |

Core 不强制所有公式使用 col。CLI 0.2.0 已移除旧客户端的已知裸字段拦截，保持原始表达式，
不需要自动补 col。管理员若仍遇到该拦截，应核对实际客户端版本和调用入口。

## Core：把公式作为 DSL 字符串解析

先按 Core README 安装独立运行环境。Core 与 fe-cli 是两个独立产品；安装 fe-cli 不会安装 Core。
在已安装 Core 的 Python 3.12 环境中使用：

```python
from factor_engine.api.dsl_parser import parse_factor

# 下面四种公式在本次计算验证中产生相同的编译 IR 和结果。
factor = parse_factor("rank(ts_mean(close,20))", name="mean20_rank")
factor = parse_factor('rank(ts_mean(col("close"),20))', name="mean20_rank")
factor = parse_factor("rank(ts_mean(close,window=20))", name="mean20_rank")
factor = parse_factor(
    'rank(ts_mean(col("close"),window=20,min_periods=20))',
    name="mean20_rank",
)
```

注意外层引号：`parse_factor("ts_mean(close,20)")` 中的 `close` 由 FE 解析，
不需要定义同名 Python 变量。直接写 Python 表达式 `ts_mean(close,20)` 时，
Python 会先查找变量 `close`；未定义时的 `NameError` 不能证明 FE DSL 不支持裸字段。
本例采用 DSL 字符串，不需要 `eval` 或导入 Polars 的 `col`。

`close` 精确绑定 `source.fields` 的逻辑键。若声明 `fields={"close": "PX"}`，
裸字段 `close` 与 `col("close")` 都读取物理列 `PX`；若只声明 `AdjClose`，
则应使用 `AdjClose` 或 `col("AdjClose")`。添加 col 不会自动补齐字段映射或复权语义。
字段名与算子名冲突、含空格等不是合法裸标识符时，应使用 `col("字段名")`。

## 可完整执行的 Core 合成示例

把下面代码保存为 `core_example.py`，用 **Core 环境的解释器**执行，例如在 Core 项目目录运行
`.venv/bin/python -I /absolute/path/core_example.py`。不使用 fe-cli 的安装环境。
代码只创建临时合成数据与临时因子湖，结束后自动清理，不读取真实行情或连接服务器。

```python
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import polars as pl

from factor_engine.api.dsl_parser import parse_factor
from factor_engine.backend.factory import build_backend
from factor_engine.runtime import FactorEngine
from factor_engine.storage.sources.polars_local_source import NativeParquetSourceV1

with TemporaryDirectory(prefix="fe-call-example-") as tmp:
    root = Path(tmp)
    data = root / "data"
    data.mkdir()
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(32)]
    rows = [
        (day, inst, float((i + 1) * scale))
        for inst, scale in (("A", 1), ("B", 2))
        for i, day in enumerate(days)
    ]
    pl.DataFrame(
        rows, schema=["ts", "inst", "PX"], orient="row",
        schema_overrides={"PX": pl.Float64},
    ).write_parquet(data / "bars.parquet")

    # 仅为合成观测序列，不代表真实交易日历或历史 PIT 认证。
    source = NativeParquetSourceV1(
        ["bars.parquet"], base_dir=data,
        fields={"close": "PX"}, universe=["A", "B"],
        load_window=(str(days[0]), str(days[-1])),
        request_window=(str(days[19]), str(days[-1])),
        pit={
            "mode": "frozen_snapshot",
            "available_at": "2025-01-01T00:00:00Z",
            "as_of": "2025-01-01T00:00:00Z",
        },
    )
    formulas = (
        "rank(ts_mean(close,20))",
        'rank(ts_mean(col("close"),20))',
        "rank(ts_mean(close,window=20))",
        'rank(ts_mean(col("close"),window=20,min_periods=20))',
    )
    with FactorEngine(build_backend(), source, run_mode="research") as engine:
        reference = None
        ir_hashes = set()
        for text in formulas:
            factor = parse_factor(text, name="mean20_rank")
            plan, analysis = engine.compile(factor)
            ir_hashes.add(plan.ir_hash)
            with engine.run(factor, plan=plan, analysis=analysis) as out:
                frame = out.result.clone()
            if reference is None:
                reference = frame
            else:
                assert frame.equals(reference)
            print(text, "rows=", frame.height)
        assert len(ir_hashes) == 1
        assert reference.height == 26
        assert reference["is_valid"].all()

        # 此时 factor 是上述最后一种写法，四种写法已验证同一计算计划。
        lake = root / "lake"
        with engine.materialize(factor, lake_root=lake) as saved:
            assert saved.materialization["status"] == "COMPLETE"
        with engine.read(factor.name, lake_root=lake) as saved:
            assert saved.result.equals(reference)
        print("PASS: four spellings, one IR, materialize/read agree")
```

预期打印四种公式各 26 行，最后打印 PASS。基础滚动 `ts_mean(close,20)` 默认要求
窗口内 20 个有效样本；本例加载 32 个观测，只请求第 20 个及之后的 13 个日期、2 个标的。
`run` 不自动保存，`materialize` 写本地因子湖，`read` 回读；这些不等于写入 DA 结果库。
冻结快照不证明真实历史 PIT，不能用本例的时间声明替代真实数据可见性证据。

Core YAML 的对应公式也可写为 `factor: {name: mean20_rank, expr: 'rank(ts_mean(close,20))'}`；
完整配置仍需真实的 `source.execution_spec()`、源目录和落值参数，不能只用这一行运行。

## 当前 fe-cli 的调用范例

使用 0.2.0，按根 README 初始化后，可直接保存裸字段公式：

```bash
fe-cli init
fe-cli factor add ret_mean20 --name "Mean return (20d)" \
  --expression 'ts_mean(ret,20)'
```

该 ID 已存在时用 `fe-cli factor update ret_mean20 --expression 'ts_mean(ret,20)'`，
或在交互终端用 `/factor edit ret_mean20`。`col("ret")` 写法也保持支持。
init 会补齐标准运行配置；获准服务器路径不同时用 configure-run 自定义，无需固定 SHA。
之后可在本机准备请求：

```bash
fe-cli prepare ret_mean20 --start 2026-10-01 --end 2026-10-30
```

确认要提交真实任务时再执行：

```bash
fe-cli --online run ret_mean20 --start 2026-10-01 --end 2026-10-30
```

如果看到 `Disk-source expressions require col(...) instead of bare fields.`，应检查是否仍在使用旧客户端。
0.2.0 保留基础语法检查，不在客户端执行公式或猜测字段映射；具体字段和算子语义由 Core 验证。
模拟模式通过不能代替真实任务验收。

## 管理员排查与核验依据

在**实际作业使用的解释器**中执行下面代码，核对加载位置，并确认最小公式可解析：

```python
import inspect
from importlib.metadata import version
from factor_engine.api.dsl_parser import parse_factor

print("factor-engine", version("factor-engine"))
print("parser", inspect.getfile(parse_factor))
print(parse_factor("ts_mean(close,20)", name="probe"))
```

版本号相同不保证工件相同；还应核对作业固定的源码/发布清单摘要。排查时保留实际命令、
完整错误和调用入口。解析通过不代表实际字段齐全，需继续用上面的完整例子或获准源验证计算。
保存定义时出现规范化的 `col()` 和具名参数，是内部保存格式，不是用户输入必须改写。

本次证据：

- Core 本地 `fe-core`，版本 0.5.1，提交 `42199795af1f045b89a1fc160b42dba2f7a79825`。
  `api/dsl_parser.py` 的裸名分支构造 ColumnRef；既有
  `tests/polars_single/test_pm_dsl_workflow.py` 定向验证 6 passed，覆盖默认 native、显式 Expr、
  四种写法计算/落湖/回读/重建、裸字段 YAML 物化及实际 native 内核调用。
  在 Core 项目可用 `.venv/bin/python scripts/test_runtime.py --profile pm-workflow` 复跑。
- 旧 CLI 0.1.1 基线 `4a488031` 的裸字段拒绝曾由定向测试证实；该历史事实不代表 0.2.0 行为。
  0.2.0 已增加四种写法的原样提交测试，冻结请求、上传包、定义指纹和 CSV 均保持原始表达式。
- 本页完整合成示例已用 Core 解释器直接执行通过：四种写法各 26 行、同一 IR、落湖/回读一致。
  以上是本机验证，不代表管理员服务器已升级；
  本次未提交远程作业，也未修改任何已发布安装包。
