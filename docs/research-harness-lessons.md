# Research Harness Lessons

本文档沉淀 Research Loop Harness 在真实运行中暴露的工程问题、根因、解决方案和遗留风险。
它记录框架研发经验，不记录具体策略知识、研究结论或生成的运行日志。

## 记录约定

新增问题时使用稳定编号，并尽量包含以下内容：

- **状态**：`open`、`mitigated` 或 `resolved`
- **发现日期**
- **现象与影响**
- **触发条件**
- **根因**
- **技术方案**
- **验证方法**
- **遗留风险或后续工作**
- **关联代码、测试或实验编号**

不要只记录最终修复。失败尝试、诊断证据和方案取舍同样重要，但应提炼结论，不要复制大段日志。

## RH-001：候选 Worktree 导入了主工作区代码

- **状态**：resolved
- **发现日期**：2026-07-18
- **现象与影响**：候选策略已经修改，但开发集指标与基准完全一致；打印模块路径后发现
  `quant_core` 来自主工作区。正式评测若沿用同一环境，会把所有候选当成基准策略评估，使接受判定失真。
- **触发条件**：项目以 editable mode 安装，源码位于 `src/`，命令从 Git worktree 执行，但进程的
  Python 搜索路径仍优先命中主工作区的 editable install。
- **根因**：仅设置子进程 `cwd` 不足以覆盖 editable install 的导入映射。
- **技术方案**：所有 OpenCode、测试和回测子进程都显式把候选 worktree 的 `src/` 放在
  `PYTHONPATH` 首位，同时保留已有 `PYTHONPATH`。
- **验证方法**：在候选 worktree 中运行真实子进程，断言策略模块的 `__file__` 位于候选路径；
  单元测试验证候选 `src/` 位于搜索路径首位。
- **遗留风险**：非 Python 命令或自行重置环境变量的子进程仍可能绕过该隔离。长期无人值守运行可考虑
  为每个候选创建独立虚拟环境，或通过固定入口执行评测。
- **关联代码**：`src/quant_core/research/runner.py::_workspace_env`

## RH-002：单轮研发退化为无界参数搜索

- **状态**：open
- **发现日期**：2026-07-18
- **现象与影响**：首轮在一个假设下执行大量临时网格和十余次 Shell 调用，运行约 14.5 分钟；
  研发过程逐渐从验证机制漂移为开发集参数挖掘，增加耗时、成本和过拟合风险。
- **触发条件**：Prompt 只要求“内部迭代”，没有定义单轮实现次数、参数组合数或提前停止条件。
- **根因**：外层 Harness 有轮数和时间预算，但单轮 Agent 拥有不受约束的 Bash 和回测能力。
- **技术方案**：
  - 每轮只允许一个可证伪机制。
  - 最多 3 次实现，每次最多 6 组配置，总计最多 18 次候选评估。
  - 候选满足开发集约束并达到目标改善后立即提交。
  - 不允许看到结果后切换到新的信号族或围绕被拒方案做宽泛局部搜索。
- **验证方法**：收紧后的一轮在第一批 6 组配置找到可行改善后立即提交，耗时约 3 分钟。
- **再次观测**：后续真实 Run 中，Agent 在一批 6 组正式配置后又执行 6 组“非正式探针”，随后
  继续下一批配置；最终 `result.json` 没有完整披露这些额外评估。另一个 Round 在首个信号表现不佳后，
  将不同特征重新命名为同一机制家族并继续搜索。两者证明 Prompt 只能缓解，不能落实预算或冻结假设。
- **遗留风险**：Agent 可以把候选回测描述为诊断、复核或非正式探针来绕过软预算；也可以在结果已知后
  改写机制边界。若成功轮的原始事件随后被压缩，最终证据不足以重建真实搜索次数。
- **后续工作**：
  - 提供唯一受控的 Development evaluator，签发 evaluation ID、记录参数指纹并硬计数。
  - 第一次评估前持久化结构化 `hypothesis`、`signal_family`、允许特征和参数范围。
  - 达到预算或偏离冻结机制时拒绝继续评测，并将违规写入永久 Round 结果。
  - 提供参数化 CLI 和示例，避免 Agent 因缺少正式入口自行拼装数据加载和回测脚本。
- **关联代码**：`src/quant_core/research/runner.py::_prompt`

## RH-003：回测成功但无标准输出，Agent 误判为失败

- **状态**：resolved
- **发现日期**：2026-07-18
- **现象与影响**：回测命令退出码为 0，但 stdout 为空。Agent 将其描述为“没有输出”，随后反复
  `find`、`ls` 并搜索指标文件，产生无效工具调用。
- **触发条件**：Evaluator 的成功结果只写入 `metrics.json`，命令本身保持静默；Prompt 只提供命令，
  没有提供格式化后的指标路径和静默语义。
- **根因**：执行契约对 Harness 是明确的，对研发 Agent 却不完整。
- **技术方案**：Prompt 显式提供本轮 development metrics 路径，并说明空 stdout 是正常成功行为，
  应直接读取指标文件。
- **验证方法**：Prompt 回归测试校验实验 ID 已正确展开到指标路径。
- **遗留风险**：其他生成文件的命令若同样静默，也应在其契约中声明产物路径。

## RH-004：自动加载与任务无关的 Skill

- **状态**：mitigated
- **发现日期**：2026-07-18
- **现象与影响**：策略研发轮次自动加载 ETF 发现、选池、数据刷新和推荐 Skill。虽然修改范围检查阻止了
  业务越界，但额外上下文可能分散推理并增加 token 消耗。
- **触发条件**：任务使用 Skill 目录中的固定股票池，名称和路径容易触发对应 Skill。
- **根因**：策略优化所需的静态输入与完整业务工作流共用目录和语义。
- **技术方案**：Prompt 明确声明股票池和缓存数据固定，禁止加载或运行发现、选池、刷新和推荐工作流；
  Scope 继续禁止修改相关路径。
- **验证方法**：Prompt 回归测试覆盖禁止语句；运行时继续检查事件日志中是否出现无关 Skill。
- **遗留风险**：Prompt 仍属于软约束。若 OpenCode 支持按任务配置 Skill allowlist，应改用能力级限制。

## RH-005：不可行 Champion 的接受语义

- **状态**：resolved
- **发现日期**：2026-07-18
- **现象与影响**：原始策略可能不满足 gate 硬约束。如果仍要求候选相对原始策略提升目标函数，
  一个已经满足全部约束、但目标值略低的候选会被错误拒绝。
- **根因**：单一“目标改善”规则没有区分可行解和不可行解。
- **技术方案**：
  - 候选始终必须通过全部 gate 硬约束。
  - Champion 不可行时，首个目标值有限且满足约束的候选直接替换 Champion。
  - Champion 可行后，候选才需要同时满足相对目标改善。
- **验证方法**：Decision 输出同时记录 `champion_constraints_passed` 和
  `relative_improvement_required`；真实运行中首个合规候选按该规则被接受。
- **遗留风险**：如果未来存在多组优先级不同的约束，需要扩展为分层可行性比较，而不是继续增加特例。

## RH-006：中断恢复会消耗一个研发轮次

- **状态**：open
- **发现日期**：2026-07-18
- **现象与影响**：为了修复 Harness 而中断正在运行的实验后，恢复逻辑会把未提交实验记录为 failed，
  并计入 `max_rounds`。这是保守且可审计的行为，但在 Harness 调试阶段会减少有效研发轮数。
- **根因**：系统无法证明中断候选是否完整，也无法安全复用未提交的 Agent 会话。
- **当前做法**：保留失败记录，不手工篡改 loop state，也不创建临时 research root。后续正常调用
  继续使用任务配置的同一根目录，由 Harness 维护 Run 编号和审计边界。
- **后续工作**：增加显式的 `research loop reset-current` 或“诊断轮不计预算”模式。该命令必须校验
  候选未提交、Champion 未变化，并保留审计记录。
- **遗留风险**：直接删除实验目录或编辑状态文件会破坏恢复语义，不应作为常规操作。

## RH-007：开发集改善不能替代 Gate 约束

- **状态**：resolved
- **发现日期**：2026-07-18
- **现象与影响**：多个候选在开发集同时改善 Sortino 和回撤，但在近期 gate 上仍出现超限回撤或换手。
- **根因**：开发期和近期市场的波动结构、拥挤程度与触发频率不同；单个全期指标无法证明跨状态泛化。
- **技术方案**：继续由 Harness 独立运行隐藏 gate，Agent 不接触精确 gate 指标；拒绝原因以约束类别
  反馈，避免将 gate 变成新的可调参开发集。
- **验证方法**：真实运行中只有满足全部 gate 约束的候选被提升，失败候选未污染 Champion。
- **遗留风险**：轮次增加后，反复接收 gate 通过/失败本身也会形成间接过拟合。需要限制总轮数，
  并在策略正式上线前使用新的、未参与循环的最终验证区间或前向观察。

## RH-008：终局报告必须读取完整 Decision 语义

- **状态**：resolved
- **发现日期**：2026-07-18
- **现象与影响**：首版终局报告能够正确列出各轮指标，但对 Champion 的接受原因表述偏泛，没有明确
  区分“原 Champion 不可行，候选通过约束即可接受”和“Champion 已可行，候选还需相对改善”。
- **根因**：报告输入只有 `accepted`/`rejected` 标签和 reasons，没有传入 decision 中的目标比较、
  Champion 可行性和逐项约束结果。模型只能根据指标自行推断判定逻辑。
- **技术方案**：每轮报告记录同时包含完整的 `decision_objective`、`decision_constraints` 和
  `decision_reasons`；Prompt 要求接受/拒绝理由以这些字段为准。
- **验证方法**：重新生成的报告明确写出
  `champion_constraints_passed=false`、`relative_improvement_required=false` 对接受判定的影响。
- **遗留风险**：未来扩展多目标或分层约束时，报告契约也必须同步升级，不能让总结模型逆向猜测规则。
- **关联代码**：`src/quant_core/research/report.py::_experiment_records`

## RH-009：终局报告必须绑定单次 Loop 的实验边界

- **状态**：resolved
- **发现日期**：2026-07-18
- **现象与影响**：复用同一 research root 再次启动 loop 时，实验目录会保留历史轮次；若报告直接
  扫描全部目录，就会把多次运行混成一次复盘，轮次统计、假设演进和 Champion 归因都可能失真。
- **根因**：实验 ID 在任务内单调递增，但首版 loop state 只记录最后一个实验，没有保存本次运行的
  完整实验集合。
- **技术方案**：任务级 Champion 与 Run 历史分离。每次 Loop 自动分配 `runs/<run>/`，本次轮次
  只写入其 `rounds/` 子目录，Run 自己保存 `state.json` 和 `report.md`。旧版平铺状态在首次访问时
  迁移到编号 Run。
- **验证方法**：连续启动两次 Loop，确认分别生成 `runs/001/rounds/001` 和
  `runs/002/rounds/001`，且两份报告均保留并只读取所属 Run。
- **遗留风险**：旧版目录若曾手工删除部分实验，只能按旧状态中可用的轮次信息迁移。
- **关联代码/测试**：`src/quant_core/research/loop.py::_record_decision`、
  `src/quant_core/research/report.py::_loop_round_ids`、`tests/test_research_report.py`

## RH-010：候选 Agent 可通过 Bash 枚举 Research Root

- **状态**：open
- **发现日期**：2026-07-18
- **现象与影响**：候选 Agent 位于隔离 worktree，但通过 Bash 使用主仓库绝对路径，仍能枚举
  `.research/<task-id>/` 下的 Champion、Run 和 Round 文件。专用读取工具拒绝了外部文件读取，
  Bash 枚举却成功。这说明 Agent 理论上可以绕过脱敏历史，直接读取旧 Gate 指标或终局报告，
  破坏 Gate 隔离。
- **触发条件**：OpenCode 配置 `external_directory=deny`，同时允许 Bash；Agent 进程的操作系统
  文件权限仍可读取主仓库。
- **根因**：worktree 隔离只控制候选代码内容，不是文件系统安全边界；工具级 external-directory
  规则没有约束 Bash 子进程访问绝对路径。
- **技术方案**：研发 Agent 必须运行在真实文件系统沙箱或容器中，只挂载候选 worktree、
  Development 数据和必要运行环境。Research Root、Gate runtime、主工作区和终局报告不得挂载。
  Gate 评估继续由独立 Harness 进程执行。
- **验证方法**：集成测试让 Agent 分别通过读取工具、Bash、Python 和符号链接尝试访问主仓库
  `.research` 与 Gate runtime，全部必须失败；同时候选测试和 Development 回测仍可运行。
- **遗留风险**：仅增加 Prompt 禁止语句、路径黑名单或 OpenCode 工具权限不足以覆盖子进程、
  解释器和符号链接绕过。
- **关联代码**：`src/quant_core/research/runner.py::_run_opencode`

## RH-011：Runner 早期失败产生幽灵 Round 并复用编号

- **状态**：resolved
- **发现日期**：2026-07-18
- **现象与影响**：真实 Run 在 `current_round` 已写入后、候选目录创建前遭遇 Git 临时索引权限错误。
  恢复逻辑将该 Round 计为失败，但没有创建 `result.json` 和 `decision.json`；下一编号只扫描物理目录，
  因而再次分配相同 ID。最终状态显示完成轮数大于唯一 Round 工件数，少执行一个真实候选，并使终局
  报告出现不存在的缺失轮次。
- **触发条件**：Managed runner 在 Round 目录创建前抛出异常，随后恢复同一 Run。
- **根因**：
  - 恢复逻辑只在 Round 目录已经存在时补写失败工件。
  - `_next_round_id` 不参考已记录的 Round ID。
  - `_record_decision` 对重复 ID 去重列表，却仍递增计数器。
- **技术方案**：
  - 恢复任何未完成 Round 时先原子创建目录，并补写失败 `result.json`、`decision.json`。
  - 新 Round 编号同时参考物理目录和状态中已保留的 ID。
  - `_record_decision` 拒绝重复 Round ID，并校验完成数、决策计数和 ID 数量一致。
- **验证方法**：新增回归测试覆盖“只有 `current_round`、没有 Round 目录”的恢复，确认失败工件落盘、
  下一轮使用新 ID，且计数严格一致；另测状态已有 ID 时不得复用。
- **遗留风险**：是否让基础设施故障占用研究预算仍属于 RH-006 的产品语义问题；本修复只保证其
  可审计且不破坏编号和状态。
- **关联代码/测试**：`src/quant_core/research/loop.py::_next_round_id`、
  `src/quant_core/research/loop.py::_record_decision`、
  `tests/test_research_loop.py`

## RH-012：报告未显式校验 Loop 状态与 Round 工件一致性

- **状态**：resolved
- **发现日期**：2026-07-18
- **现象与影响**：幽灵 Round 使状态声称完成 6 轮，但只有 5 个 Round 工件。终局报告模型只能根据
  不一致输入推断“另有一轮记录不足”，无法明确这是 Harness 状态损坏，也存在虚构缺失轮次内容的风险。
- **根因**：报告输入同时传递汇总计数和 Round 列表，却没有确定性的完整性检查或显式警告。
- **技术方案**：报告生成前检查完成数与 ID 数量、接受/拒绝/失败计数总和、Round 目录及
  `result.json`/`decision.json` 是否存在；将所有异常写入 `loop.integrity_warnings`。报告 Prompt
  要求在总览中明确标记 Harness 状态一致性问题，并禁止为缺失工件虚构研究内容。
- **验证方法**：新增报告回归测试构造计数与 ID 数量不一致的状态，确认警告进入报告输入且禁止虚构
  指令存在；正常状态的警告列表为空。
- **遗留风险**：该修复负责检测和诚实报告，不能自动修复已有损坏状态；状态写入侧仍需依赖 RH-011
  的不变量保护。
- **关联代码/测试**：`src/quant_core/research/report.py::_loop_integrity_warnings`、
  `tests/test_research_report.py`

## RH-013：单一 Development 汇总指标缺少稳健性和行为诊断

- **状态**：open
- **发现日期**：2026-07-18
- **现象与影响**：多轮候选在完整 Development 区间显著改善目标或回撤，但在 Gate 上没有相对改善；
  另有候选在 Gate 上与 Champion 指标完全相同，说明新增逻辑没有改变该区间的实际交易。当前决策只能
  给出目标未改善，不能区分跨阶段失效、触发样本不足和行为等价。
- **根因**：提交前只使用单一 Development 全期汇总；Decision 契约没有候选与 Champion 的订单、
  持仓、暴露和机制触发差异。
- **技术方案**：
  - 由 Harness 固定 Development 子区间或滚动折叠，报告各段目标、约束和贡献方向。
  - 要求最低触发样本和跨段一致性，切片定义不能由 Agent 选择。
  - Gate 评估记录与 Champion 不同的信号日、订单、持仓、总暴露和机制触发次数。
  - 行为完全一致时输出显式 `behaviorally_equivalent`，而不只报告目标相同。
- **验证方法**：构造只在单个 Development 子段有效、Gate 完全不触发、以及订单完全相同的候选，
  分别验证稳健性门槛和行为差异分类。
- **遗留风险**：增加 Development 切片本身也扩大可见反馈面，必须预先固定并限制指标，避免形成
  新的参数挖掘目标。
- **关联模块**：`src/quant_core/research/runner.py`、`src/quant_core/research/evaluator.py`

## RH-014：成功工件压缩会删除搜索预算与假设漂移证据

- **状态**：open
- **发现日期**：2026-07-18
- **现象与影响**：实时事件显示 Agent 执行了永久 `result.json` 未披露的额外 Development 探针，
  但成功轮在解析后删除 OpenCode 事件，Loop 结束时又删除成功日志。若没有外部实时监督，无法从耐久
  工件重建真实评估次数、参数集合或假设切换过程。
- **根因**：压缩逻辑假设结构化 Agent 总结已经完整，而评估调用和假设边界尚不由 Harness 记录。
- **技术方案**：在删除原始事件前，必须先由 Harness 持久化不可变的 Development evaluation
  manifest，包含 evaluation ID、机制 ID、参数指纹、命令、指标、开始/结束时间和预算计数。若检测到
  超限、越界或总结不一致，保留原始事件和失败诊断。
- **验证方法**：让 fake Agent 执行额外评估或提交不完整总结，确认 manifest 仍完整、违规被标记且
  原始事件未被压缩；正常成功轮可删除重复日志。
- **遗留风险**：在受控 evaluator 落地前，保留所有原始事件只能缓解审计缺口，会显著增加存储量，
  也不能阻止违规发生。
- **关联代码**：`src/quant_core/research/runner.py::run_once`、
  `src/quant_core/research/workspace.py::compact_artifacts`

## 新问题模板

```markdown
## RH-NNN：简短标题

- **状态**：open
- **发现日期**：YYYY-MM-DD
- **现象与影响**：
- **触发条件**：
- **根因**：
- **技术方案**：
- **验证方法**：
- **遗留风险**：
- **后续工作**：
- **关联代码/测试/实验**：
```
