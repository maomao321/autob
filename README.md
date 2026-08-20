# AutoQuant：Binance Stocks 桌面量化程序

这是按 `需求文档.txt` 需求实现的 Python/Qt 桌面程序。界面基于 PySide6，可在 Windows、macOS 和 Linux 上运行。程序支持按股票独立启动、停止，供应商和策略均通过工厂隔离；本版接入 Binance Stocks Trading、ChatGPT 和 DeepSeek，并实现“当日方向 + 五分钟 MA5/前一根 K 线突破”策略。

程序默认使用 `PAPER` 模拟交易。除非在界面中主动切换为 `REAL` 并再次确认，否则不会向 Binance 提交真实订单。

## 安装和运行

要求 Python 3.10 或更高版本。安装项目时会一并安装 PySide6 Qt 运行时。

```powershell
py -m pip install -e .
py -m autoquant
```

Windows 可以双击 `run.bat`，macOS 可以双击 `run.command`（首次使用若无执行权限，先运行 `chmod +x run.command packaging/build_macos.sh`）。这两个启动脚本只从当前项目源码运行 `autoquant`，优先使用项目 `.venv`，再尝试系统 Python；不会启动 `dist` 中的构建产物。安装后还可执行：

```powershell
autoquant
```

### Windows EXE

安装了 Python 的 Windows 电脑可运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build_exe.ps1
```

构建脚本使用独立环境安装项目依赖和 PyInstaller，然后生成无控制台窗口的 `dist\windows\AutoQuant.exe`。如需指定 Python，可追加 `-PythonExe C:\path\to\python.exe`。

### macOS App

macOS 应用必须在 macOS 电脑上本机构建，不能在 Windows 上交叉编译：

```bash
chmod +x packaging/build_macos.sh run.command
./packaging/build_macos.sh
```

构建完成后可直接打开 `dist/macos/AutoQuant.app`；`run.command` 始终从源码启动。如需指定 Python，可设置 `PYTHON_EXE=/path/to/python3` 后运行构建脚本。若要分发给其他 Mac，仍需按 Apple 要求完成代码签名和公证。

## 使用步骤

1. 首次运行保留 `PAPER` 模式。
2. 打开“运行配置”页，配置交易 API、交易模式、MA 周期、金额与风控；需要大模型决策时再选择 `CHATGPT`、`DEEPSEEK` 或 `DUAL`，并填写相应 API Key。
3. 返回“交易监控”页，添加一个或多个大写美股代码，例如 `AAPL`、`NVDA`、`TSLA`。
4. 选择股票后点击“启动所选”，或点击“全部启动”。每只股票有独立运行器，可分别停止。
5. 查看状态、程序持仓、持仓均价、未决订单、今日买入金额和日志。

非敏感配置保存在 `%LOCALAPPDATA%\AutoQuant\config.json`。API Key/Secret 不写入配置文件；可以每次在界面填写，也可在启动前设置环境变量：

```powershell
$env:BINANCE_API_KEY = "你的 API Key"
$env:BINANCE_API_SECRET = "你的 API Secret"
$env:OPENAI_API_KEY = "你的 OpenAI API Key"
$env:DEEPSEEK_API_KEY = "你的 DeepSeek API Key"
py -m autoquant
```

### 大模型今日开仓方向

- `DISABLED`：保持原策略，使用 Binance 当日日线相对开盘价判断 `LONG / SHORT / FLAT`。
- `CHATGPT`：调用 OpenAI Responses API，使用严格 JSON Schema 返回方向、置信度、依据和风险。
- `DEEPSEEK`：调用 DeepSeek Chat Completions JSON Output；空响应最多重试一次。
- `DUAL`：并行调用两者，只有方向一致且各自置信度都不低于配置阈值时才放行；其他情况为 `FLAT`。

每个股票在收到新的 Binance 日 K 线后只决策一次。上下文包含近期新闻标题、`SPY/QQQ` 大盘日线统计、个股日线统计以及 Binance 当前日线；公开历史价格来自 Nasdaq 网站的历史行情端点，新闻来自 Google News RSS。大模型只能设置方向过滤器，不能修改金额、止损止盈，也不能直接下单。上下文获取失败、API 超时、返回字段非法、模型拒绝、低置信度或双模型不一致时，程序会安全降级为 `FLAT`，当天不新开仓。

OpenAI/DeepSeek Key 只驻留内存，不写入配置文件。股票代码、新闻和走势摘要会发送给选择的大模型服务；同时程序会访问 Nasdaq 与 Google News。使用前应自行确认这些服务的账号权限、费用、数据许可和所在地区可用性。公开数据和模型结论都可能延迟或错误，不能替代交易所行情、人工核对或风险管理。

订单保护账本保存在 `%LOCALAPPDATA%\AutoQuant\orders.sqlite3`。其中保存订单标识、方向、交易日、请求金额、成交数量和程序持仓，不保存 API 凭据。该文件用于恢复交易次数、持仓和资金限额，不能在交易运行期间删除或修改。

“交易监控”页顶部每 30 秒刷新一次账户概览，也可以手动刷新：

- “Binance 账户总金额”调用官方钱包余额接口，将全部已激活钱包折算为 USDC 后汇总；需要 API Key 和 Secret，与当前选择 PAPER 或 REAL 无关。
- “程序已实现盈亏”与“程序未实现盈亏”只统计本程序账本中能够确认的成交，不代表 Binance 全账户盈亏。已实现盈亏计入新版本记录到的交易手续费；未实现盈亏使用当前买卖报价中间价估算。
- 旧版本订单没有保存手续费，因此升级前订单的历史盈亏可能略高于实际值。缺少持仓报价时，程序显示“行情不可用”，不会展示不完整的未实现盈亏。

## 策略定义

程序只在一根 5 分钟 K 线已经收盘时评估信号：

- 大模型模式启用时使用当日模型决策作为方向；大模型模式关闭时，日线当前价高于开盘价为 `LONG`、低于开盘价为 `SHORT`、相等时不交易。
- 做多：上一根 5 分钟收盘价不高于上一时点 MA，当前收盘价上穿当前 MA，并且当前收盘价突破前一根最高价。
- 做空/卖出：上一根 5 分钟收盘价不低于上一时点 MA，当前收盘价下穿当前 MA，并且当前收盘价跌破前一根最低价。实盘中，该信号只用于平掉程序确认的多头，不建立空头。
- 每根收盘 K 线只评估一次。每日次数限制用于入场，风险退出不受入场次数限制。
- 程序持续用 5 分钟行情更新检查止损和止盈；触发后使用程序账本记录的全部多头数量发出 SELL。
- 入场 BUY 直接使用配置的“买入金额”，没有多头持仓的 PAPER SELL 直接使用配置的“卖出数量”。退出已有多头时始终卖出程序记录的实际持仓数量。
- 新的日 K 线到达时会清空上一交易日的 5 分钟预热数据；乱序 K 线和不属于当前日 K 线时间范围的数据不会参与计算。

计算 MA 交叉需要 `MA 周期 + 1` 根已收盘 5 分钟 K 线。本版使用 Binance Stocks 公开 WebSocket；当前股票 REST 市场数据接口没有提供历史 K 线端点，因此程序刚启动时需要预热。例如 MA5 需要收到 6 根已收盘 5 分钟 K 线。

“当日”边界以 Binance 推送的日 K 线开始/结束时间（当前 WebSocket 字段为 `t` / `ct`）为准。策略不会混用不同日 K 线的数据，也不会额外假设纽约常规交易时段；若 Binance 的日 K 线包含扩展时段，该时段同样属于当日策略范围。

## 实盘注意事项

- `REAL` 会提交真实的 `MARKET` 订单。BUY 直接使用“买入金额(USDC)”作为 `notional`，并在每次启动股票前展示实际单笔金额、再次要求确认。
- Binance Stocks API 的市价 BUY 接受 `notional`，市价 SELL 接受 `quantity`；程序不提供额外的合约倍数或杠杆配置。交易所返回的 `multiplierUp` / `multiplierDown` 是价格带限制，不是杠杆倍数。
- Binance 要求账户先接受 US Equity Disclaimer，否则下单会返回错误 `486410`。程序不会代替用户自动接受法律声明。
- API Key 需要开启交易权限。股票代码还必须在 Binance Stocks 当前可交易列表中。
- 当前版本实盘为安全优先的长仓模式：没有程序持仓时阻止 SELL；存在程序确认的多头时，策略 SELL、止损或止盈会平掉该持仓。已有多头时也会阻止重复 BUY 加仓。
- “程序持仓”只包含本程序能够确认成交的订单，不代表 Binance 账户的全部真实持仓。若在 Binance 网页或其他客户端手工交易，必须人工核对两边状态。
- 账户总金额与程序盈亏的统计口径不同：前者来自 Binance 全钱包余额，后者只来自本地程序订单账本，不能相减后作为全账户收益。
- 买入金额必须小于或等于“单笔上限”和“每日买入上限”；所有股票共享“每日买入上限”，资金通过 SQLite 即时事务原子预留。即使同时打开多个程序进程，同股票的未决订单、重复持仓和超量卖出也会在事务内再次拦截。股票数量最多 20 只。
- 下单前会按交易所返回的 `stepSize`、数量上下限和金额上下限规范化或校验订单。
- 下单接受后会查询订单详情并保存成交数量/均价；未到终态的订单会阻止同股票继续下单，并在后续行情消息及重启时继续查询。
- 网络超时后的订单状态可能未知。任何未知实盘订单都会硬锁该股票，跨日也不会自动解除。只有登录 Binance 核对订单并处理对应持仓后，才能点击“核对后解除未知订单锁”。
- “停止所选并平仓”与“全部停止并平仓”会先阻止新的策略订单，再按本地账本记录的全部多头数量提交 `MARKET SELL`；PAPER 记录模拟成交，REAL 提交真实订单。存在未知/未决订单、交易所禁止 SELL 或平仓结果无法确认时，程序会进入错误状态且不会盲目重试，必须登录 Binance 核对并处理真实持仓。
- 超过“信号有效期”的行情不会下单，`recvWindow` 被限制在 5000 毫秒以内。MARKET 订单仍可能受到价差、流动性、停牌和网络延迟影响，止损价不保证等于最终成交价。
- 行情和界面队列均有容量上限，REST 公共信息使用短期缓存并限制并发，避免多股票运行时无限占用内存或集中请求接口。
- 在投入真实资金前，应完成模拟验证、限额配置、账户权限检查和风险评估。
- 大模型判断不是收益保证。建议先在 `PAPER + DUAL` 模式观察多个交易日，并审阅日志中的方向、置信度、依据和风险，再考虑实盘。

## Binance 官方接口依据

- Stocks Trading 介绍：<https://developers.binance.com/en/docs/products/stocks/introduction>
- 模块通用规则：<https://developers.binance.com/en/docs/products/stocks/general-info>
- 快速开始与签名下单：<https://developers.binance.com/en/docs/products/stocks/quick-start>
- WebSocket 连接与流名称：<https://developers.binance.com/en/docs/products/stocks/websocket-streams-general-info>
- REST 行情与交易规则：<https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/market-data>
- REST 下单接口：<https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/trade>
- 钱包总余额：<https://developers.binance.com/docs/wallet/asset/query-user-wallet-balance>

## 大模型官方接口依据

- OpenAI Responses API 与 Java SDK：<https://developers.openai.com/api/docs/libraries>
- OpenAI Structured Outputs：<https://developers.openai.com/api/docs/guides/structured-outputs>
- DeepSeek JSON Output：<https://api-docs.deepseek.com/guides/json_mode/>
- DeepSeek 模型与 API Base URL：<https://api-docs.deepseek.com/quick_start/pricing/>

## 项目结构与扩展

- `autoquant/providers/`：行情和交易供应商接口；当前实现 `BinanceStocksProvider`。
- `autoquant/ai_decision.py`：新闻/走势上下文、ChatGPT/DeepSeek 客户端、结构化结果校验与双模型共识。
- `autoquant/experience.py`：外部 Excel/CSV 交易与K线导入、形态标准化、本地经验库及 OpenAI Vector Store 上传。
- `autoquant/strategies/`：策略接口；当前实现 `FiveMinuteBreakoutStrategy`。
- `autoquant/engine.py`：每只股票的独立运行器及启动/停止控制。
- `autoquant/app.py`：PySide6/Qt 桌面界面。

添加供应商或策略时，实现相应抽象接口，并在 `autoquant/engine.py` 的工厂函数中注册即可。

## 交易经验库

“交易经验库”页只读取用户选择的外部文件，不读取本程序订单账本。交易记录和K线形态都支持 UTF-8 `.csv` 或现代 Excel `.xlsx`（读取第一个工作表），两类文件可以单独导入，也可以一起导入。旧版 `.xls` 请先在 Excel 中另存为 `.xlsx`。

交易记录的必填字段如下；字段名也支持对应的中文名称：

```text
trade_id,symbol,side,entry_time,exit_time,entry_price,exit_price,quantity,fee,notes
T001,AAPL,LONG,2026-08-13T09:35:00Z,2026-08-13T09:45:00Z,100,110,2,1,突破后回踩
```

其中 `symbol`、`entry_time`、`exit_time`、`entry_price`、`exit_price`、`quantity` 必填；`trade_id`、`side`、`fee`、`entry_fee`、`exit_fee`、`notes`、`setup`、`market_regime`、`tags` 可选。总手续费 `fee` 与分项手续费 `entry_fee/exit_fee` 二选一。`side` 支持 `LONG/SHORT`、`BUY/SELL` 或 `多/空`，缺省按 `LONG` 计算。程序根据方向、价格、数量和手续费重新计算盈亏，不采信文件中的结果标签。

K线形态文件示例：

```text
pattern_id,pattern_name,symbol,close_time,open,high,low,close,volume,interval
T001,突破前缩量,AAPL,2026-08-13T09:34:00Z,100,102,99,101,1200,1m
```

`open`、`high`、`low`、`close` 和时间字段必填；`symbol` 与 `pattern_id` 至少提供一个。`close_time` 也可以写作 `close_time_ms`、`timestamp`、`time`、`datetime` 或 `date`，都按K线收盘后的可用时间解释。使用 `open_time` 或 `open_time_ms` 时必须提供类似 `1m`、`5m`、`1h` 的 `interval`，程序会换算为收盘时间。

当K线的 `pattern_id`（也可命名为 `trade_id`）与交易记录的 `trade_id` 相同时，K线会关联到该笔交易；没有编号时则按 `symbol` 关联。关联交易时只使用开仓前已经收盘的K线，防止未来数据泄漏。若只导入K线文件，建议每种典型形态使用独立的 `pattern_id` 分组。

导入结果可以保存到 `%LOCALAPPDATA%\AutoQuant\external_trade_experiences.json`，也可以使用“运行配置”页内存中的 OpenAI API Key 上传到新的或已有的 Vector Store。上传是显式操作，API Key 不会写入经验文件。为避免混入旧的本地订单数据，这个外部经验库使用独立文件名；DeepSeek 后续可由本地检索器选取相关经验后随决策请求一起发送。

## 测试

测试不会连接 Binance，也不会下单：

```powershell
py -m unittest discover -s tests -v
```
