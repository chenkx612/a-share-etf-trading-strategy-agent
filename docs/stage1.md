# 阶段一实现说明

阶段一是人工可用的最小多因子系统，不包含 agent。系统通过 CLI 暴露确定性命令，输出文件可被人工检查，也可在后续阶段被 agent 读取。

## 数据流

1. `data update` 使用 AKShare 获取 ETF 池和日线行情。
2. 原始行情写入 `data/raw/etf_daily.parquet`，缺少 parquet 引擎时写入 CSV。
3. `factor compute` 计算基础因子并写入 `outputs/factors/factors.*`。
4. `backtest run` 生成选股、持仓、收益曲线、指标和 Markdown 报告。
5. `recommend today` 生成指定日期的人工调仓建议。

以上路径都相对于 CLI 的 `--root` 工作目录。项目根目录只放源码、文档、测试和研究工作区；当前 Sharpe 单因子策略工作区位于 `workspaces/sharpe_single/`。

## 设计边界

- 数据源优先 AKShare，后续可通过 provider 适配其他数据源。
- 回测默认使用内置向量化等权日频调仓逻辑，项目依赖声明 `bt`，后续可替换为 bt 组合树实现。
- 阶段一不做实盘交易、不做订单执行、不做 agent 自动改策略。
