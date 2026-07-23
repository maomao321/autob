# AutoQuant：Binance Stocks 桌面量化程序

这是按 `需求文档.txt` 需求实现的 Windows/Python 桌面程序。程序支持按股票独立启动、停止，供应商和策略均通过工厂隔离；本版接入 Binance Stocks Trading，并实现“日线方向 + 五分钟 MA5/前一根 K 线突破”策略。

程序默认使用 `PAPER` 模拟交易。除非在界面中主动切换为 `REAL` 并再次确认，否则不会向 Binance 提交真实订单。

## 安装和运行

要求 Python 3.10 或更高版本。Windows 官方 Python 通常自带 Tkinter。

```powershell
py -m pip install -e .
py -m autoquant
```

也可以双击 `run.bat` 启动。安装后还可执行：

```powershell
autoquant
```

### Windows EXE

仓库已提供单文件 Windows 版本：`dist\AutoQuant.exe`，无需另行安装 Python。也可在安装了 Python 的 Windows 电脑上重新构建：

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build_exe.ps1 -PythonExe py
```

构建脚本会安装项目依赖和 PyInstaller，然后生成无控制台窗口的 `dist\AutoQuant.exe`。

## 使用步骤

1. 首次运行保留 `PAPER` 模式。
2. 打开“运行配置”页，配置 API、交易模式、MA 周期、买入金额、每日账户买入上限、止损/止盈和信号有效期。
3. 返回“交易监控”页，添加一个或多个大写美股代码，例如 `AAPL`、`NVDA`、`TSLA`。
4. 选择股票后点击“启动所选”，或点击“全部启动”。每只股票有独立运行器，可分别停止。
5. 查看状态、程序持仓、持仓均价、未决订单、今日买入金额和日志。

非敏感配置保存在 `%LOCALAPPDATA%\AutoQuant\config.json`。API Key/Secret 不写入配置文件；可以每次在界面填写，也可在启动前设置环境变量：

```powershell
$env:BINANCE_API_KEY = "你的 API Key"
$env:BINANCE_API_SECRET = "你的 API Secret"
py -m autoquant
```

订单保护账本保存在 `%LOCALAPPDATA%\AutoQuant\orders.sqlite3`。其中保存订单标识、方向、交易日、请求金额、成交数量和程序持仓，不保存 API 凭据。该文件用于恢复交易次数、持仓和资金限额，不能在交易运行期间删除或修改。

“交易监控”页顶部每 30 秒刷新一次账户概览，也可以手动刷新：

- “Binance 账户总金额”调用官方钱包余额接口，将全部已激活钱包折算为 USDC 后汇总；需要 API Key 和 Secret，与当前选择 PAPER 或 REAL 无关。
- “程序已实现盈亏”与“程序未实现盈亏”只统计本程序账本中能够确认的成交，不代表 Binance 全账户盈亏。已实现盈亏计入新版本记录到的交易手续费；未实现盈亏使用当前买卖报价中间价估算。
- 旧版本订单没有保存手续费，因此升级前订单的历史盈亏可能略高于实际值。缺少持仓报价时，程序显示“行情不可用”，不会展示不完整的未实现盈亏。

## 策略定义

程序只在一根 5 分钟 K 线已经收盘时评估信号：

- 日线当前价高于日线开盘价时，方向为 `LONG`；低于开盘价时，方向为 `SHORT`；相等时不交易。
- 做多：上一根 5 分钟收盘价不高于上一时点 MA，当前收盘价上穿当前 MA，并且当前收盘价突破前一根最高价。
- 做空/卖出：上一根 5 分钟收盘价不低于上一时点 MA，当前收盘价下穿当前 MA，并且当前收盘价跌破前一根最低价。实盘中，该信号只用于平掉程序确认的多头，不建立空头。
- 每根收盘 K 线只评估一次。每日次数限制用于入场，风险退出不受入场次数限制。
- 程序持续用 5 分钟行情更新检查止损和止盈；触发后使用程序账本记录的全部多头数量发出 SELL。
- 新的日 K 线到达时会清空上一交易日的 5 分钟预热数据；乱序 K 线和不属于当前日 K 线时间范围的数据不会参与计算。

计算 MA 交叉需要 `MA 周期 + 1` 根已收盘 5 分钟 K 线。本版使用 Binance Stocks 公开 WebSocket；当前股票 REST 市场数据接口没有提供历史 K 线端点，因此程序刚启动时需要预热。例如 MA5 需要收到 6 根已收盘 5 分钟 K 线。

“当日”边界以 Binance 推送的日 K 线 `openTime/closeTime` 为准。策略不会混用不同日 K 线的数据，也不会额外假设纽约常规交易时段；若 Binance 的日 K 线包含扩展时段，该时段同样属于当日策略范围。

## 实盘注意事项

- `REAL` 会提交真实的 `MARKET` 订单。BUY 使用“买入金额(USDC)”作为 `notional`，并在每次启动股票前再次要求确认。
- Binance 要求账户先接受 US Equity Disclaimer，否则下单会返回错误 `486410`。程序不会代替用户自动接受法律声明。
- API Key 需要开启交易权限。股票代码还必须在 Binance Stocks 当前可交易列表中。
- 当前版本实盘为安全优先的长仓模式：没有程序持仓时阻止 SELL；存在程序确认的多头时，策略 SELL、止损或止盈会平掉该持仓。已有多头时也会阻止重复 BUY 加仓。
- “程序持仓”只包含本程序能够确认成交的订单，不代表 Binance 账户的全部真实持仓。若在 Binance 网页或其他客户端手工交易，必须人工核对两边状态。
- 账户总金额与程序盈亏的统计口径不同：前者来自 Binance 全钱包余额，后者只来自本地程序订单账本，不能相减后作为全账户收益。
- 单笔买入金额必须小于“单笔上限”；所有股票共享“每日买入上限”，资金通过 SQLite 即时事务原子预留。即使同时打开多个程序进程，同股票的未决订单、重复持仓和超量卖出也会在事务内再次拦截。股票数量最多 20 只。
- 下单前会按交易所返回的 `stepSize`、数量上下限和金额上下限规范化或校验订单。
- 下单接受后会查询订单详情并保存成交数量/均价；未到终态的订单会阻止同股票继续下单，并在后续行情消息及重启时继续查询。
- 网络超时后的订单状态可能未知。任何未知实盘订单都会硬锁该股票，跨日也不会自动解除。只有登录 Binance 核对订单并处理对应持仓后，才能点击“核对后解除未知订单锁”。
- “停止策略(不平仓)”只阻止新的策略订单，不会撤单或平仓。需要立即处理持仓时，应直接登录 Binance 操作。
- 超过“信号有效期”的行情不会下单，`recvWindow` 被限制在 5000 毫秒以内。MARKET 订单仍可能受到价差、流动性、停牌和网络延迟影响，止损价不保证等于最终成交价。
- 行情和界面队列均有容量上限，REST 公共信息使用短期缓存并限制并发，避免多股票运行时无限占用内存或集中请求接口。
- 在投入真实资金前，应完成模拟验证、限额配置、账户权限检查和风险评估。

## Binance 官方接口依据

- Stocks Trading 介绍：<https://developers.binance.com/en/docs/products/stocks/introduction>
- 模块通用规则：<https://developers.binance.com/en/docs/products/stocks/general-info>
- 快速开始与签名下单：<https://developers.binance.com/en/docs/products/stocks/quick-start>
- WebSocket 连接与流名称：<https://developers.binance.com/en/docs/products/stocks/websocket-streams-general-info>
- REST API 参考：<https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/market-data>
- 钱包总余额：<https://developers.binance.com/docs/wallet/asset/query-user-wallet-balance>

## 项目结构与扩展

- `autoquant/providers/`：行情和交易供应商接口；当前实现 `BinanceStocksProvider`。
- `autoquant/strategies/`：策略接口；当前实现 `FiveMinuteBreakoutStrategy`。
- `autoquant/engine.py`：每只股票的独立运行器及启动/停止控制。
- `autoquant/app.py`：Tkinter 桌面界面。

添加供应商或策略时，实现相应抽象接口，并在 `autoquant/engine.py` 的工厂函数中注册即可。

## 测试

测试不会连接 Binance，也不会下单：

```powershell
py -m unittest discover -s tests -v
```
