# Quant Agent 项目阶段规划

## 阶段 0：明确边界

目标：先定义系统约束，避免过早复杂化。

关键约定：
- 市场：A 股 ETF
- 周期：日频数据，每天调仓
- 策略：横截面多因子选股
- 股票池：A 股所有规模大于百亿的 ETF
- 输出：选股列表、目标权重、回测报告
- 评估：收益、超额收益、最大回撤、夏普、IC、RankIC、换手率等

## 阶段 1：人工可用的最小多因子系统

目标：打通数据、因子、选股、回测、推荐闭环。

关键能力：
- 获取并更新 A 股 ETF 日线数据。
- 计算基础因子，如动量、反转、波动率、成交量、换手率。
- 按因子分数选出 top N 股票。
- 每日等权调仓回测。
- 生成每日选股建议。

## 阶段 2：研究工作流工程化

目标：把人工研究流程变成稳定、可复现的工具链。

关键能力：
- 策略配置文件标准化。
- 因子定义标准化。
- 回测报告标准化。
- 实验结果可追溯。
- 支持命令行运行回测和推荐。

建议产物：
- `strategy.yaml`
- `factors.py` 或 `factor_config.yaml`
- `backtest_report.json`
- `backtest_report.md`
- `positions.csv`
- `orders.csv`

关键节点：
- M6：策略配置文件标准化。
- M7：回测报告标准化。
- M8：实验结果可追溯。
- M9：单命令跑通回测和推荐。

## 阶段 3：受控 agent 接入

目标：让 agent 基于已有工具链做策略研发、回测、比较和报告。

关键原则：
- agent 可以新增策略和因子。
- agent 可以运行回测和生成报告。
- agent 不应覆盖 baseline。
- agent 不应删除历史实验。
- agent 不应直接生成实盘订单。

建议 skills：
- `data_diagnosis_skill`
- `factor_research_skill`
- `factor_implementation_skill`
- `backtest_skill`
- `evaluation_skill`
- `strategy_iteration_skill`
- `report_skill`

关键节点：
- M10：agent 能读取策略配置和回测报告。
- M11：agent 能提出因子改进方案。
- M12：agent 能新增实验并运行回测。
- M13：agent 能比较 baseline 和新策略。
- M14：agent 能生成下一轮迭代建议。

## 阶段 4：Qlib / ML 多因子深化

目标：从规则打分升级到机器学习多因子模型。

关键能力：
- 跑通 Qlib A 股数据流程。
- 跑通 Alpha158 + LightGBM baseline。
- 支持 rolling / walk-forward 训练。
- 使用模型预测分数构建组合。
- 比较规则策略和 ML 策略表现。

关键节点：
- M15：Qlib Alpha158 baseline 可运行。
- M16：walk-forward 训练流程可运行。
- M17：模型预测分数可用于选股。
- M18：agent 能比较规则策略和 ML 策略。

## 阶段 5：模拟盘和实盘对接

目标：在研究系统稳定后，接入组合跟踪、风控和交易执行。

关键能力：
- 每日持仓跟踪。
- 目标持仓到调仓订单转换。
- 风控检查，如单票权重、行业暴露、换手率、黑名单。
- 模拟盘运行。
- 小资金实盘，先保留人工审批。

关键节点：
- M19：纸面组合每日跟踪可用。
- M20：模拟盘稳定运行 1-3 个月。
- M21：小资金实盘，人工确认订单。
- M22：半自动或自动执行。

## 当前优先级

优先完成阶段 1 和阶段 2。

在基础工具链稳定前，不急于让 agent 自由研发策略。agent 的效果取决于数据、回测、评估和实验管理是否可靠。
