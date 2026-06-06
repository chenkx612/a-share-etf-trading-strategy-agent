# Quant Agent 使用手册：从策略设计到上线

本文档面向人工研究员，描述一个 ETF 多因子策略从设计、数据准备、因子计算、回测评估、参数优化到每日选股清单生成的完整流程。

当前阶段是“人工可用”的阶段一：系统提供确定性的 CLI、文件产物和报告，不引入 agent，也不直接实盘下单。这里的“上线”指进入人工审核后的模拟盘或人工执行流程。

## 0. 环境准备

建议先安装项目依赖：

```bash
python3 -m pip install -e ".[dev]"
```

如果暂时没有安装 `pyarrow`，系统会把本地表降级写成 CSV；安装完整依赖后默认使用 Parquet。

常用命令格式：

```bash
python3 -m quant_agent.cli <模块> <命令> [参数]
```

所有命令默认在当前项目目录读写数据。也可以用 `--root` 指定独立工作目录：

```bash
python3 -m quant_agent.cli --root /path/to/workdir factor compute --start 2024-01-01 --end 2024-12-31
```

建议把研究数据和源码分开管理。当前 Sharpe 单因子策略工作区位于：

```bash
python3 -m quant_agent.cli --root workspaces/sharpe_single factor compute --start 2026-05-01 --end 2026-05-31
```

下文中的 `data/...` 和 `outputs/...` 都是相对于 `--root` 工作目录的路径。

## 1. 股票池设定与数据更新

阶段一默认市场是 A 股 ETF，股票池和策略配置解耦。默认股票池是 `large-etf`，规则是“规模大于 100 亿的 ETF”；另有内置股票池 `sector-rotation`，来自行业轮动 ETF 配置。系统优先通过 AKShare 获取 ETF 列表和日线行情。

### 1.1 自动生成股票池

运行：

```bash
python3 -m quant_agent.cli data update --start 2024-01-01 --end 2024-12-31
```

系统会执行：

- 从 AKShare 拉取 ETF 列表。
- 尝试按基金规模过滤。
- 下载每只 ETF 的日线行情。
- 写入标准化行情表。

主要产物：

- `data/universe/etf_universe.*`：ETF 股票池。
- `data/raw/etf_daily.*`：原始日线行情。
- `data/processed/etf_daily.*`：标准化后的日线行情。
- `data/universe/large_etf_universe.*`：默认内置股票池。

指定内置行业轮动股票池：

```bash
python3 -m quant_agent.cli data update \
  --universe-name sector-rotation \
  --start 2024-01-01 \
  --end 2024-12-31
```

`sector-rotation` 默认使用前复权 `qfq` 下载行情，以匹配 `~/quant` 的数据口径；如需覆盖可显式传入 `--adjust`。

标准行情字段：

```text
date, symbol, name, open, high, low, close, volume, amount, turnover
```

### 1.2 人工维护股票池

如果 AKShare 的规模字段不可用，或者研究员希望固定一个基准股票池，可以手工维护 CSV：

```csv
symbol,name,fund_size
510300,沪深300ETF,10000000000
510500,中证500ETF,10000000000
159915,创业板ETF,10000000000
```

然后运行：

```bash
python3 -m quant_agent.cli data update \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --universe data/universe/etf_universe.csv
```

### 1.3 每日数据更新

每日收盘后更新到当天：

```bash
python3 -m quant_agent.cli data update --start 2024-01-01 --end 2026-05-31
```

当前实现会把新数据和本地旧数据按 `date + symbol` 去重合并。人工上线前应检查命令输出里的 `data warnings`，重点关注重复行、缺失收盘价、字段缺失。

## 2. 因子定义与计算

阶段一内置基础多因子，定义在 `quant_agent/factors/core.py`。

当前内置因子：

- `momentum_20`：近 20 日收益率，表示中短期动量。
- `sharpe_20`：近 20 日收益率 / 近 20 日日收益波动率，表示简化夏普单因子。
- `sharpe_25`：近 25 日收益率 / 近 25 日日收益波动率，供 `sector-sharpe` 使用。
- `reversal_5`：近 5 日收益率取负，表示短期反转。
- `volatility_20`：近 20 日日收益波动率。
- `amount_mean_20`：近 20 日成交额均值。
- `turnover_mean_20`：近 20 日换手率均值。

计算命令：

```bash
python3 -m quant_agent.cli factor compute --start 2024-01-01 --end 2024-12-31
```

主要产物：

- `outputs/factors/factors.*`

该文件会保留行情字段和因子字段，是后续打分、选股、回测、推荐的统一输入。

### 2.1 新增因子

新增因子时遵守三条规则：

- 只使用当前日期及之前的数据，避免未来函数。
- 按 `symbol` 分组后 rolling 或 pct_change。
- 把新因子列加入 `FACTOR_COLUMNS`，并在策略配置里设置权重。

示例方向：

```python
df["momentum_60"] = grouped["close"].pct_change(60)
```

然后在 `quant_agent/config.py` 的 `StrategyConfig.factor_weights` 里加入：

```python
"momentum_60": 1.0
```

## 3. 策略定义与回测

阶段一策略是横截面多因子打分策略。

默认逻辑：

- 每个交易日对所有 ETF 做横截面 z-score。
- 按因子权重合成 `score`。
- 选择综合分最高的 Top N。
- Top N 等权配置。
- 夜间产生目标权重，下一交易日开盘按开盘价调仓。
- 默认交易成本 `0.1%`。

默认权重定义在 `quant_agent/config.py`：

```python
{
    "momentum_20": 1.0,
    "reversal_5": 1.0,
    "volatility_20": -1.0,
    "amount_mean_20": 0.5,
    "turnover_mean_20": 0.5,
}
```

也可以使用两类夏普因子策略：

- `sharpe-single`：当前项目的横截面打分版本，因子为 `sharpe_20`，按 z-score 后 Top N 选股，下一交易日开盘成交。
- `sector-sharpe`：在当前框架下尽量贴近 `~/quant` 中的行业轮动夏普策略，参数为 `M=5, N=25, K=100, corr_threshold=0.9, stop_loss_pct=0.1, fee_rate=0.0003`；使用原始 `sharpe_25` 排序、相关性过滤、上一信号资产单日止损剔除。回测使用当前项目统一的夜间信号、下一交易日开盘成交逻辑。
- `sector-factor-threshold`：在 `sector-sharpe` 基础上增加因子下限过滤，默认 `factor_lower_bound=0.0`；只有 `sharpe_25 > factor_lower_bound` 的资产才会入选。仓位按固定槽位 `1 / top_n` 分配，不足 `top_n` 只时剩余资金留现金；若当天没有资产过线，下一交易日开盘清仓。

运行回测：

```bash
python3 -m quant_agent.cli backtest run \
  --start 2024-03-01 \
  --end 2024-12-31 \
  --top-n 10 \
  --fee-rate 0.001 \
  --run-id baseline_top10
```

运行当前项目夏普单因子回测：

```bash
python3 -m quant_agent.cli backtest run \
  --strategy sharpe-single \
  --universe-name sector-rotation \
  --start 2024-03-01 \
  --end 2024-12-31 \
  --run-id sharpe_single
```

`--strategy` 只决定因子权重、持仓数量和费率等策略参数；`--universe-name` 决定候选股票池。同一策略可以切换到 `large-etf` 或 `sector-rotation` 运行。

在当前框架下运行行业轮动夏普策略：

```bash
python3 -m quant_agent.cli backtest run \
  --strategy sector-sharpe \
  --universe-name sector-rotation \
  --start 2023-04-01 \
  --end 2026-05-31 \
  --run-id sector_sharpe
```

运行因子下限行业轮动策略：

```bash
python3 -m quant_agent.cli backtest run \
  --strategy sector-factor-threshold \
  --universe-name sector-rotation \
  --start 2023-04-01 \
  --end 2026-05-31 \
  --factor-lower-bound 0.0 \
  --run-id sector_factor_threshold
```

主要产物：

- `outputs/backtests/baseline_top10/orders.csv`：每日选股和目标权重。
- `outputs/backtests/baseline_top10/positions.csv`：回测持仓。
- `outputs/backtests/baseline_top10/daily_returns.csv`：每日收益、成本、换手。
- `outputs/backtests/baseline_top10/equity_curve.csv`：净值曲线。
- `outputs/backtests/baseline_top10/metrics.json`：核心指标。
- `outputs/reports/baseline_top10.md`：Markdown 报告。

可单独重建报告：

```bash
python3 -m quant_agent.cli report build --run-id baseline_top10
```

人工评估时至少检查：

- 总收益和年化收益。
- 最大回撤。
- 夏普比率。
- 平均换手率。
- IC 和 RankIC。
- 最新推荐是否集中在少数主题或流动性较差 ETF。

## 4. 策略超参数优化

阶段一提供一个简单网格搜索命令。核心原则是：只在训练期选择参数，再在验证期确认，避免过拟合。

### 4.1 可优化参数

优先优化这些参数：

- `top_n`：持仓数量，例如 5、10、15、20。
- `fee_rate`：交易成本，例如 0.0005、0.001、0.002。
- `factor_lower_bound`：因子下限，仅用于 `sector-factor-threshold`，例如 -0.5、0.0、0.5。
- `corr_threshold`：行业轮动相关性过滤阈值，例如 0.8、0.9、0.95。
- `stop_loss_pct`：行业轮动单日止损阈值，例如 0.06、0.08、0.10。
- 因子权重：例如提高动量权重、降低换手率权重。
- 因子窗口：例如 `momentum_20` 改成 20/60 日组合。

### 4.2 网格搜索

先固定因子权重，只比较 Top N 和交易成本：

```bash
python3 -m quant_agent.cli optimize grid \
  --strategy sharpe-single \
  --start 2024-03-01 \
  --end 2024-12-31 \
  --top-n 3,5,10 \
  --fee-rate 0.0003,0.001 \
  --objective sharpe \
  --run-id sharpe_grid
```

优化因子下限行业轮动策略：

```bash
python3 -m quant_agent.cli optimize grid \
  --strategy sector-factor-threshold \
  --universe-name sector-rotation \
  --start 2024-03-01 \
  --end 2024-12-31 \
  --top-n 3,5 \
  --factor-lower-bound -0.5,0.0,0.5 \
  --corr-threshold 0.8,0.9 \
  --stop-loss-pct 0.08,0.1 \
  --objective sharpe \
  --run-id sector_factor_threshold_grid
```

对比：

```text
outputs/optimizations/sharpe_grid/results.csv
outputs/optimizations/sharpe_grid/best.json
```

选择参数时不要只看收益。更稳妥的排序方式：

1. 最大回撤不能超过人工可接受范围。
2. 换手率不能高到被交易成本吞掉。
3. RankIC 应为正且相对稳定。
4. 收益、夏普、回撤三者综合更好。

### 4.3 训练期和验证期

建议至少拆成两段：

```text
训练期：2021-01-01 到 2023-12-31
验证期：2024-01-01 到 2024-12-31
```

流程：

1. 在训练期跑多组参数。
2. 选择 1 到 3 组候选参数。
3. 在验证期重跑候选参数。
4. 如果验证期表现明显退化，回到因子设计阶段，而不是继续扩大搜索范围。

阶段一上线标准建议：

- 训练期和验证期都能跑通。
- 验证期最大回撤可接受。
- 交易频率符合人工执行能力。
- 最新选股清单可解释。
- 报告和参数记录可追溯。

## 5. 生成当日选股清单

当日推荐依赖两个前置条件：

1. 行情已经更新到目标日期。
2. 因子已经计算到目标日期。

每日流程：

```bash
python3 -m quant_agent.cli data update --start 2024-01-01 --end 2026-05-31
python3 -m quant_agent.cli factor compute --start 2024-01-01 --end 2026-05-31
python3 -m quant_agent.cli recommend today --date 2026-05-31 --top-n 10
```

产物：

```text
outputs/recommendations/2026-05-31_large-etf.csv
```

推荐文件包含：

- `date`：信号日期。
- `symbol`：ETF 代码。
- `name`：ETF 名称。
- 各因子值。
- `score`：综合得分。
- `target_weight`：目标权重。

人工使用方式：

1. 打开当日推荐 CSV。
2. 检查是否有停牌、异常成交、明显错误价格。
3. 检查目标权重是否合计为 1。
4. 对比当前持仓，人工决定是否调仓。
5. 阶段一只输出目标权重，不自动生成实盘订单。

## 6. 一条完整流程示例

下面是一条从设计到上线前推荐的最小流程：

```bash
# 1. 更新股票池和行情
python3 -m quant_agent.cli data update --start 2021-01-01 --end 2024-12-31

# 2. 计算因子
python3 -m quant_agent.cli factor compute --start 2021-01-01 --end 2024-12-31

# 3. 训练期回测
python3 -m quant_agent.cli backtest run \
  --start 2021-03-01 \
  --end 2023-12-31 \
  --top-n 10 \
  --run-id train_top10

# 4. 验证期回测
python3 -m quant_agent.cli backtest run \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --top-n 10 \
  --run-id valid_top10

# 5. 生成最新选股清单
python3 -m quant_agent.cli recommend today --date 2024-12-31 --top-n 10
```

夏普单因子推荐：

```bash
python3 -m quant_agent.cli recommend today \
  --strategy sector-sharpe \
  --universe-name sector-rotation \
  --date 2024-12-31
```

上线前保留这些文件作为审计记录：

- 股票池文件：`data/universe/etf_universe.*`
- 因子结果：`outputs/factors/factors.*`
- 回测报告：`outputs/reports/train_top10.md`、`outputs/reports/valid_top10.md`
- 回测指标：`outputs/backtests/*/metrics.json`
- 当日推荐：`outputs/recommendations/YYYY-MM-DD_<universe-name>.csv`
