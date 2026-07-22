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
2. 添加一个或多个大写美股代码，例如 `AAPL`、`NVDA`、`TSLA`。
3. 配置 MA 周期、买入金额、卖出数量和每日最多交易次数。
4. 选择股票后点击“启动所选”，或点击“全部启动”。每只股票有独立运行器，可分别停止。
5. 查看状态、日线方向、最新价、MA、预热进度、今日交易次数和日志。

非敏感配置保存在 `%LOCALAPPDATA%\AutoQuant\config.json`。API Key/Secret 不写入配置文件；可以每次在界面填写，也可在启动前设置环境变量：

```powershell
$env:BINANCE_API_KEY = "你的 API Key"
$env:BINANCE_API_SECRET = "你的 API Secret"
py -m autoquant
```

订单保护账本保存在 `%LOCALAPPDATA%\AutoQuant\orders.sqlite3`。其中只保存订单标识、方向、交易日和状态，不保存 API 凭据。该文件用于在程序重启后恢复每日交易次数，不能在交易运行期间删除。

## 策略定义

程序只在一根 5 分钟 K 线已经收盘时评估信号：

- 日线当前价高于日线开盘价时，方向为 `LONG`；低于开盘价时，方向为 `SHORT`；相等时不交易。
- 做多：上一根 5 分钟收盘价不高于上一时点 MA，当前收盘价上穿当前 MA，并且当前收盘价突破前一根最高价。
- 做空/卖出：上一根 5 分钟收盘价不低于上一时点 MA，当前收盘价下穿当前 MA，并且当前收盘价跌破前一根最低价。
- 每根 K 线只评估一次，并受“每日最多交易”参数限制。
- 新的日 K 线到达时会清空上一交易日的 5 分钟预热数据；乱序 K 线和不属于当前日 K 线时间范围的数据不会参与计算。

计算 MA 交叉需要 `MA 周期 + 1` 根已收盘 5 分钟 K 线。本版使用 Binance Stocks 公开 WebSocket；当前股票 REST 市场数据接口没有提供历史 K 线端点，因此程序刚启动时需要预热。例如 MA5 需要收到 6 根已收盘 5 分钟 K 线。

“当日”边界以 Binance 推送的日 K 线 `openTime/closeTime` 为准。策略不会混用不同日 K 线的数据，也不会额外假设纽约常规交易时段；若 Binance 的日 K 线包含扩展时段，该时段同样属于当日策略范围。

## 实盘注意事项

- `REAL` 会提交真实的 `MARKET` 订单。BUY 使用“买入金额(USDC)”作为 `notional`，并在每次启动股票前再次要求确认。
- Binance 要求账户先接受 US Equity Disclaimer，否则下单会返回错误 `486410`。程序不会代替用户自动接受法律声明。
- API Key 需要开启交易权限。股票代码还必须在 Binance Stocks 当前可交易列表中。
- 当前 Binance Stocks MARKET 下单接口没有提供策略建立空头所需的借券参数，因此 `PAPER` 可模拟 SHORT/SELL，但 `REAL` 会安全阻止该类 SELL 信号，不会把它误当成普通卖出持仓。
- 下单前会按交易所返回的 `stepSize`、数量上下限和金额上下限规范化或校验订单。
- 网络超时后的订单状态可能未知。程序会在发出请求前写入本地账本，未知订单不会自动重试，并会占用当日交易次数；有 `orderId` 的未完成实盘订单会在下次启动时查询 Binance 状态。仍应到 Binance 人工核对状态未知订单。
- 在投入真实资金前，应完成模拟验证、限额配置、账户权限检查和风险评估。

## Binance 官方接口依据

- Stocks Trading 介绍：<https://developers.binance.com/en/docs/products/stocks/introduction>
- 模块通用规则：<https://developers.binance.com/en/docs/products/stocks/general-info>
- 快速开始与签名下单：<https://developers.binance.com/en/docs/products/stocks/quick-start>
- WebSocket 连接与流名称：<https://developers.binance.com/en/docs/products/stocks/websocket-streams-general-info>
- REST API 参考：<https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/market-data>

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
