# AutoQuant：Binance Stocks / USDⓈ-M Futures 前后端量化系统

系统已经拆分为独立后端服务和 PySide6/Qt 前端。后端持有 Binance Stocks Trading 与 Binance USDⓈ-M Futures 连接、策略线程、风控、配置和 SQLite 订单账本，可以在服务器持续运行；前端只通过带鉴权的 REST API 读取状态和发送控制命令，关闭前端不会停止策略或触发平仓。程序支持按标的独立启动、停止，供应商和策略均通过工厂隔离，并实现“手动方向或 GPT/DeepSeek 今日方向 + 五分钟 MA7/MA25 与前两根 K 线突破 + 可选大模型时机审核”策略。

程序默认使用 `PAPER` 模拟交易。除非在界面中主动切换为 `REAL` 并再次确认，否则不会向 Binance 提交真实订单。

## 安装和运行

要求 Python 3.10 或更高版本。安装项目时会一并安装 PySide6 Qt 运行时。

```bash
python3 -m pip install -e .
```

先在服务器启动后端：

```bash
export AUTOQUANT_API_TOKEN='请替换为足够长的随机令牌'
autoquant-server --host 127.0.0.1 --port 8765
```

再在前端电脑启动 Qt 客户端：

```bash
export AUTOQUANT_SERVER_URL='http://127.0.0.1:8765'
export AUTOQUANT_API_TOKEN='与服务器相同的令牌'
autoquant
```

同一台电脑运行时，可以分别双击 `run-server.command` 和 `run.command`；Windows 对应 `run-server.bat` 和 `run.bat`。默认仅监听本机回环地址，本机模式可以不设置令牌。

macOS 的两个 `.command` 启动器会主动查找 pyenv（包括 Homebrew 和 `~/.pyenv` 的常见安装位置），因此从 Finder 双击时不依赖交互式 Shell 的初始化配置。先在项目目录选择一个 Python 3.10 或更高版本：

```bash
pyenv local 3.14.7  # 可替换为本机已经安装的其他 3.10+ 版本
chmod +x run.command run-server.command
```

首次启动会用 pyenv 当前选择的解释器创建 `.venv` 并安装依赖。如果已有 `.venv` 来自系统 Python 或其他解释器，启动器会将它保留为带时间戳的 `.venv.backup-*` 后再重建。也可以通过 `PYTHON_EXE=/完整路径/python ./run.command` 显式覆盖解释器。

### 远程服务器部署

生产环境建议让后端继续监听 `127.0.0.1`，再通过带 HTTPS 的反向代理或 SSH 隧道访问。不要把无 TLS 的交易接口直接暴露到公网。若确实使用 `--host 0.0.0.0`，服务会强制要求设置 `AUTOQUANT_API_TOKEN`。前端也会默认拒绝连接非本机的明文 HTTP 地址；仅受信任内网临时调试可显式设置 `AUTOQUANT_ALLOW_INSECURE_HTTP=1`。

Linux 可参考 `packaging/autoquant.service.example` 配置 systemd。后端收到退出信号时只停止本进程内的行情线程，不会自动提交平仓订单；已启动的 PAPER 策略会记录在 `running.json` 并在服务重启后恢复。REAL 策略只有明确设置 `AUTOQUANT_RESTORE_REAL=1` 才会自动恢复，避免服务器重启后未经授权恢复实盘交易。

后端健康检查：

```bash
curl http://127.0.0.1:8765/health
```

### REST API

除 `/health` 外，设置令牌后所有接口都要求请求头 `Authorization: Bearer <token>`。当前前端使用以下版本化接口：

- `GET /api/v1/config`、`PUT /api/v1/config`：读取或更新服务器配置，读取结果只返回凭据掩码。
- `GET /api/v1/status?after_log=<序号>`：增量读取运行快照与日志。
- `POST /api/v1/runners/{symbol}/start`：按手动方向或已保存的大模型模式启动策略；默认使用本地配置中的 OpenAI/DeepSeek/Qwen Key，也支持随启动请求临时覆盖。
- `POST /api/v1/runners/{symbol}/stop`：停止策略，可明确要求按服务器账本持仓平仓。
- `POST /api/v1/stop-targets`：查询运行中、持仓中或存在阻塞订单的目标。
- `POST /api/v1/connection/check`：由服务器检查 Binance API 和股票代码。
- `POST /api/v1/account/overview`：由服务器查询钱包并计算程序账本盈亏。
- `GET /api/v1/futures/rankings?limit=20`：读取 Binance USDT 永续合约滚动 24 小时股票涨/跌幅榜与加密涨/跌幅榜。
- `GET /api/v1/runners/{symbol}/unknown-orders`、`POST /api/v1/runners/{symbol}/resolve-unknown`：查询或人工确认后解除未知实盘订单锁。
- `POST /api/v1/backtest/downloads`、`GET /api/v1/backtest/downloads`：提交 180 天多周期历史行情下载并查询持久化进度。
- `POST /api/v1/backtest/runs`、`GET /api/v1/backtest/runs`：提交策略回测并查询持久化结果。
- `GET /api/v1/backtest/trades?run_id=<批次ID>`：读取指定回测批次持久化的逐笔开平仓明细。
- `GET /api/v1/backtest/bars/export`、`POST /api/v1/backtest/bars/import`：按标的导出或导入历史 K 线 ZIP 数据包。
- `DELETE /api/v1/backtest/bars`：按行情源和标的删除已持久化 K 线及下载/导入记录，保留历史回测结果。

控制类接口不会因为 HTTP 客户端断开而取消已经启动的策略。停止并平仓仍必须由前端显式调用，关闭前端本身只会停止状态轮询。

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
2. 打开“运行配置”页，选择 `binance_stocks` 或 `binance_futures`，配置交易 API、交易模式、金额与风控。五分钟突破策略固定使用 MA7/MA25；Futures 杠杆默认为 `1x`。
3. 需要大模型时，在“运行配置”勾选“启用大模型决策”，选择 `CHATGPT`、`DEEPSEEK`、`QWEN` 或 `DUAL`，并填写对应 Key 和模型名。
4. 打开“合约池”页查看 USDT 永续合约的滚动 24 小时股票涨幅榜、股票跌幅榜、加密涨幅榜和加密跌幅榜；仅在该页面打开期间，左侧候选合约池每分钟同步涨跌幅，右侧四个榜单每 30 分钟自动刷新，也可手动刷新。后端会优先复用 60 秒行情缓存，避免多个前端或相邻刷新重复请求 Binance。在任一榜单右键可加入合约池；在合约池右键可带入标的跳转到策略回测，或将标的加入并选中到交易监控页，也可移除。合约池最多保存 100 个标的，跳转交易监控后仍需用户确认启动，不会自动发出订单。
5. 返回“交易监控”页，添加一个或多个标的代码（例如 Stocks 的 `AAPL`；Futures 输入 `BTC` 会自动保存为 `BTCUSDT`）。大模型开关关闭时，为每个标的选择 `LONG`、`SHORT` 或 `FLAT`；开关开启时该手动值被忽略。
6. 选择标的后点击“启动所选”。每个标的有独立运行器，可分别停止。
7. 查看状态、程序持仓、持仓均价、未决订单、本次量化开仓金额和日志。大模型模式会记录今日方向、时机 `ENTER/WAIT`、置信度、决策耗时与风险。
8. 打开“交易记录”页，可按标的、开仓/平仓类型和模拟/实盘模式查询持久化成交记录。
9. 打开“AI 决策”页，可按标的和今日方向/开仓时机阶段查询历史决策，并查看最终结果、完整结构化输入、原始 API 输出、总决策耗时和模型响应时间。
10. 打开“策略回测”页，指定 Futures 标的并下载最近 180 天范围内可用的日线、5 分钟及 1 分钟 K 线；标的历史不足 180 天时以实际返回数量为准，下载完成后选择策略执行回测。

交易监控表格的“手动方向”在大模型开关关闭时生效，支持 `LONG`、`SHORT` 或 `FLAT`。该选择在启动标的时读取并锁定，并随“保存配置”写入配置文件。`FLAT` 表示禁止该标的产生普通开仓信号。大模型开关开启时，后端强制使用模型方向，不会回退到手动方向下单。

配置和订单账本只保存在后端服务器的 `%LOCALAPPDATA%\AutoQuant`（Windows）或 `~/.autoquant`（Linux/macOS）。在交易监控页新增或确认移除标的后，前端会立即通过鉴权接口将标的列表写入服务器配置，重启后仍保持一致；移除最后一个标的也受支持，历史交易记录不会随标的移除。界面中尚未保存的其他参数不会因此写入。Binance、OpenAI、DeepSeek 和 Qwen 凭据不会通过读取配置接口明文回传给前端；界面用掩码表示服务器已有凭据。点击“保存配置”会通过鉴权接口更新服务器配置。也可将凭据留空，并在启动后端前设置环境变量：

```powershell
$env:BINANCE_API_KEY = "你的 API Key"
$env:BINANCE_API_SECRET = "你的 API Secret"
$env:OPENAI_API_KEY = "你的 OpenAI API Key"
$env:DEEPSEEK_API_KEY = "你的 DeepSeek API Key"
$env:DASHSCOPE_API_KEY = "你的阿里云百炼 API Key"
$env:AUTOQUANT_API_TOKEN = "前后端共享的随机令牌"
py -m autoquant_backend
```

### 手动开仓方向

- `LONG`：只允许五分钟做多信号。
- `SHORT`：只允许五分钟做空信号；`binance_futures` 可在 PAPER 和 REAL 模式建立空头，`binance_stocks` 会拒绝做空。
- `FLAT`：不产生普通开仓信号。

### 大模型开仓决策

- `CHATGPT`：通过 OpenAI Responses API 和 JSON Schema 结构化输出决策。
- `DEEPSEEK`：通过 DeepSeek Chat Completions JSON Output 决策。
- `QWEN`：通过阿里云百炼 OpenAI 兼容 Chat Completions 接口和 JSON Output 决策。默认模型为 `qwen-plus`；北京地域新工作区可把界面中的 Qwen Chat 接口改为 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions`。
- `DUAL`：两个模型并行请求。今日方向必须一致，候选入场也必须同时返回 `ENTER`，否则不开仓。
- 配置页按当前模式显示对应模型卡片；`DUAL` 同时显示 OpenAI 与 DeepSeek，避免无关供应商参数混在一起。推理设置会按接口分别转换：OpenAI 使用 `reasoning.effort`，DeepSeek 使用 `thinking.type + reasoning_effort`，Qwen 使用 `enable_thinking + reasoning_effort`。关闭 OpenAI 推理设置时不发送推理参数，以兼容不支持推理控制的模型。
- 每个交易日首先结合近期新闻、SPY/QQQ 大盘走势、个股走势与当前日线生成 `LONG / SHORT / FLAT`。
- 今日方向判断固定提供最近 30 根日线 OHLC（开盘、最高、最低、收盘）数据；五分钟策略产生候选突破信号后，模型根据今日日线、配置数量的最近五分钟 OHLC、MA 和突破原因返回 `ENTER / WAIT`。时机 K 线数量默认为 60，可在 10～300 根之间配置。
- 任意网络失败、数据缺失、响应格式错误、置信度低于阈值或双模型分歧，都会安全降级为 `FLAT/WAIT`，不会绕过模型改用手动方向开仓。
- 每次今日方向和开仓时机审核都会把最终结果、结构化市场输入及所有原始模型响应持久化到 `orders.sqlite3`。重试与双模型输出会保留为同一决策记录中的多份原始响应；记录不包含 API Key。
- 大模型模式、模型名、置信度、新闻窗口、时机 K 线数量以及 OpenAI/DeepSeek/Qwen Key 会写入后端本地 `config.json`，读取接口只返回掩码；密钥不会写入 `running.json`。配置留空时仍可使用后端环境变量，服务重启后可直接使用已保存或环境变量中的 Key 恢复 AI 运行器。

Binance API Key/Secret 与 OpenAI/DeepSeek/Qwen Key 会在点击“保存配置”后写入后端服务器配置，请保护该文件的访问权限。OpenAI Key 仍可用于手动上传交易经验库。

订单保护账本保存在后端服务器的 `orders.sqlite3`。其中保存订单标识、方向、交易日、请求金额、成交价格、成交数量、手续费、平仓收益、程序持仓以及 AI 决策历史，不保存 API 凭据。“交易记录”和“AI 决策”页直接查询该持久化账本，重启前后数据保持一致；交易记录页不会展示订单号。该文件用于恢复交易次数、持仓和资金限额，不能在后端运行期间删除或修改。

### 策略回测

- 最多 180 天的多周期分页下载当前支持 `binance_futures`。不会以固定 K 线数量判定成败；标的上市不足 180 天时，只要各周期返回了有效数据就按实际数量完成。行情按行情源、标的、周期和开盘时间幂等写入 `orders.sqlite3`，重复下载会更新同一根 K 线。
- 下载和回测均由后端后台线程执行，关闭前端不会中断已提交任务；“策略回测”页每 5 秒刷新任务进度。
- 回测直接使用最近一次完成的下载或导入记录所覆盖的实际时间范围与实际样本数，不要求凑满 180 天；样本不足以产生策略信号时仍会正常完成，并持久化为 0 笔交易。
- 每条回测结果右侧提供蓝色链接式“回测明细”入口，悬停显示手掌并加下划线。弹窗分为“收益曲线”和“交易明细”两个页签：曲线页显示累计盈亏蓝线及盈利/亏损交易点（绿/红），鼠标在绘图区任意横向位置移动或点击都会定位到最近一笔交易，并显示方向、开平仓时间与价格、单笔/累计盈亏及退出原因；明细页展示完整逐笔表格和原始策略信号原因，每个单元格悬停时都会显示该列完整内容。这些数据与回测汇总一同保存在 `backtest_trades` 中。超大明细的图表最多均匀抽样 2000 个点，但表格和累计计算仍覆盖查询到的全部交易。
- 当前可选择 `five_minute_breakout`。回测使用持久化日线判断前两日方向、5 分钟收盘产生策略信号，并以 1 分钟 K 线检查配置中的止损和止盈；同一分钟同时触及止损和止盈时按止损优先处理。
- 回测快照使用提交时的开仓金额、每日交易次数和止盈止损配置，均线固定为 MA7/MA25。批次汇总及逐笔回测成交都会持久化，但配置快照会清空 Binance、OpenAI、DeepSeek 和 Qwen 凭据。
- 收益率以配置的单笔开仓金额作为初始回测资金基数，即 `总盈亏 ÷ 单笔开仓金额 × 100%`；最大回撤从该初始资金出发按逐笔累计权益峰值计算。总盈亏、胜负数和最大回撤同时保存，历史结果不会因后来修改运行配置而改变；界面中的金额和百分比统一显示两位小数。
- 当前回测不计交易手续费、资金费率和滑点，结果用于策略逻辑验证，不应视为真实收益承诺。
- “导出 K 线”会生成包含 `manifest.json` 以及日线、5分钟、1分钟 CSV 的 ZIP 数据包；“导入 K 线”要求页面标的与数据包标的一致。导入会校验文件路径、格式版本、行情源、时间顺序、OHLCV、各周期数量及 128 MB 上传限制，成功后可直接用于回测。
- 在“历史 K 线下载记录”表中右键任意记录，可以按该行的标的和行情源导出 K 线，或在二次确认后删除该标的全部持久化 K 线及下载/导入记录。进行中的下载不能删除，已有回测结果不会随行情删除。

“交易监控”页顶部每 30 秒刷新一次账户概览，也可以手动刷新：

- `binance_stocks` 的“Binance 账户总金额”调用官方钱包余额接口，将全部已激活钱包折算为 USDC 后汇总；`binance_futures` 显示 Futures 账户的 USDT 余额。两者都需要 API Key 和 Secret，与当前选择 PAPER 或 REAL 无关。
- “程序已实现盈亏”与“程序未实现盈亏”只统计本程序账本中能够确认的成交，不代表 Binance 全账户盈亏。已实现盈亏计入新版本记录到的交易手续费；未实现盈亏使用当前买卖报价中间价估算。
- 旧版本订单没有保存手续费，因此升级前订单的历史盈亏可能略高于实际值。缺少持仓报价时，程序显示“行情不可用”，不会展示不完整的未实现盈亏。

## 策略定义

程序只在一根 5 分钟 K 线已经收盘时评估信号：

- 大模型开关关闭时，开仓方向完全使用表格中启动前选定的手动方向；开关开启时，使用模型的当日方向，且普通入场必须再通过模型时机审核。风险退出不受模型时机审核阻塞。
- 做多：最近 25 根已收盘 5 分钟 K 线计算的 MA7 高于 MA25；第一根（最新已收盘）K 线收盘价高于第二根；当前最新价严格高于第二根 K 线最高价。
- 做空：最近 25 根已收盘 5 分钟 K 线计算的 MA7 低于 MA25；第一根（最新已收盘）K 线收盘价低于第二根；当前最新价严格低于第二根 K 线最低价。Futures 空仓时用 SELL 建立空头；Stocks 不支持该开仓方向。
- 每根收盘 K 线只评估一次。每日次数限制用于入场，风险退出不受入场次数限制。
- 程序持续用 5 分钟行情更新检查止损和止盈；多头触发后发出 SELL，空头触发后发出 BUY，均只减掉程序账本记录的当前净持仓。
- 多空入场都使用配置的“开仓金额”作为名义金额；退出时始终使用程序记录的实际持仓数量。任何方向尚未平仓时，新的同向或反向开仓都会被拒绝。
- 程序以实时 5 分钟 K 线的 UTC 日期划分交易日；日期变化时清空上一日的指标数据，乱序 K 线不会参与计算。

计算 MA7/MA25 需要 25 根已收盘 5 分钟 K 线。Futures 启动实时流前会通过 REST 获取最近 30 根已收盘历史 K 线用于预热指标，策略使用其中最新 25 根计算 MA7/MA25；AI 模式在收到当日日线后也会尝试回补当日五分钟 K 线。预热期间的历史信号会被丢弃，不会补下启动前的订单；进入实时流后，当前未收盘 5 分钟 K 线的最新价可以触发突破，同一根 K 线最多产生一次候选信号。若样本仍不足，继续由实时 K 线补齐。交易监控表按标的分别显示已实现收益与未实现收益，并提供逐行启动、停止图标按钮。

## 实盘注意事项

- `REAL` 会提交真实的 `MARKET` 订单，并在每次启动标的前展示单笔金额、杠杆和风控参数，再次要求确认。
- Binance Stocks 的市价 BUY 直接发送配置的买入金额作为 `notional`，市价 SELL 发送 `quantity`。
- Binance USDⓈ-M Futures 的多空开仓都会按“开仓金额 ÷ 实时参考价”换算合约数量，再按 `MARKET_LOT_SIZE` 向下取整；该金额表示仓位名义金额，不会再乘以杠杆。杠杆设置控制 Binance 对该标的使用的初始杠杆和保证金要求，默认 `1x`，允许 `1–125x`，实际可用上限仍由交易所和标的决定。
- Futures 实盘首单前会查询持仓模式并调用杠杆接口。当前版本仅支持单向持仓模式：LONG 用 BUY 开多，SHORT 用 SELL 开空；多头用带 `reduceOnly=true` 的 SELL 平仓，空头用带 `reduceOnly=true` 的 BUY 平仓。若账户处于双向持仓模式，程序会在发单前拒绝交易。
- Binance 要求账户先接受 US Equity Disclaimer，否则下单会返回错误 `486410`。程序不会代替用户自动接受法律声明。
- API Key 需要开启对应产品的交易权限；使用 Futures 前还必须先开通 Futures 账户。标的代码必须处于对应市场的可交易状态。
- 当前版本使用安全优先的单向净持仓：同一标的只能持有程序多头或程序空头之一；持仓未清零时，账本会拒绝同向加仓和反向开仓。Stocks 保持仅多头，Futures 支持多空。
- “程序持仓”只包含本程序能够确认成交的订单，不代表 Binance 账户的全部真实持仓。若在 Binance 网页或其他客户端手工交易，必须人工核对两边状态。
- 账户总金额与程序盈亏的统计口径不同：前者来自 Binance 全钱包余额，后者只来自本地程序订单账本，不能相减后作为全账户收益。
- 开仓金额必须小于或等于“单笔上限”和“每日开仓上限”；所有标的及多空方向共享“每日开仓上限”，资金通过 SQLite 即时事务原子预留。即使同时打开多个程序进程，同标的的未决订单、重复/反向持仓和超量减仓也会在事务内再次拦截。标的数量最多 20 个。
- 下单前会按交易所返回的 `stepSize`、数量上下限和金额上下限规范化或校验订单。
- 下单接受后会查询订单详情并保存成交数量/均价；未到终态的订单会阻止同股票继续下单，并在后续行情消息及重启时继续查询。
- 网络超时后的订单状态可能未知。任何未知实盘订单都会硬锁该股票，跨日也不会自动解除。只有登录 Binance 核对订单并处理对应持仓后，才能点击“核对后解除未知订单锁”。
- “停止所选并平仓”与“全部停止并平仓”会先阻止新的策略订单，再按本地账本记录的净持仓提交减仓单：多头为 `MARKET SELL`，空头为 `MARKET BUY`；PAPER 记录模拟成交，REAL 提交真实订单。存在未知/未决订单、交易所禁止对应方向或平仓结果无法确认时，程序会进入错误状态且不会盲目重试，必须登录 Binance 核对并处理真实持仓。
- 超过“信号有效期”的行情不会下单，`recvWindow` 被限制在 5000 毫秒以内。MARKET 订单仍可能受到价差、流动性、停牌和网络延迟影响，止损价不保证等于最终成交价。
- 行情和界面队列均有容量上限，REST 公共信息使用短期缓存并限制并发，避免多股票运行时无限占用内存或集中请求接口。
- 在投入真实资金前，应完成模拟验证、限额配置、账户权限检查和风险评估。
- 手动方向和大模型决策都不是收益保证。建议先在 `PAPER` 模式观察多个交易日并审阅信号日志，再考虑实盘。

## Binance 官方接口依据

- USDⓈ-M Futures 快速开始：<https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/quick-start>
- USDⓈ-M Futures K 线流：<https://developers.binance.info/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market>
- USDⓈ-M Futures REST 示例与端点：<https://github.com/binance/binance-cli/blob/master/examples/derivatives-trading-usds-futures.md>
- Stocks Trading 介绍：<https://developers.binance.com/en/docs/products/stocks/introduction>
- 模块通用规则：<https://developers.binance.com/en/docs/products/stocks/general-info>
- 快速开始与签名下单：<https://developers.binance.com/en/docs/products/stocks/quick-start>
- WebSocket 连接与流名称：<https://developers.binance.com/en/docs/products/stocks/websocket-streams-general-info>
- REST 行情与交易规则：<https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/market-data>
- REST 下单接口：<https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/trade>
- 钱包总余额：<https://developers.binance.com/docs/wallet/asset/query-user-wallet-balance>

## 大模型官方接口依据

- OpenAI Responses API：<https://developers.openai.com/api/reference/resources/responses/methods/create>
- OpenAI Structured Outputs：<https://developers.openai.com/api/docs/guides/structured-outputs>
- DeepSeek JSON Output：<https://api-docs.deepseek.com/guides/json_mode/>
- DeepSeek Chat Completions：<https://api-docs.deepseek.com/api/create-chat-completion/>
- DeepSeek 模型与 API Base URL：<https://api-docs.deepseek.com/>
- 阿里云百炼 OpenAI 兼容 Chat：<https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions>
- Qwen 结构化 JSON 输出：<https://help.aliyun.com/zh/model-studio/qwen-structured-output>

## 项目结构与扩展

- `frontend/autoquant_frontend/`：PySide6/Qt 界面、远程 HTTP 客户端和交易经验库功能，只通过后端 API 操作交易状态。
- `backend/autoquant_backend/`：常驻服务、交易引擎、策略、Provider、风控和 SQLite 订单账本，不依赖前端代码。
- `shared/autoquant_shared/`：前后端共同使用的配置结构、校验规则和 API 数据模型。
- `backend/autoquant_backend/providers/`：行情和交易供应商接口；当前实现 `BinanceStocksProvider` 与 `BinanceFuturesProvider`。
- `backend/autoquant_backend/strategies/`：策略接口；当前实现 `FiveMinuteBreakoutStrategy`。

添加供应商或策略时，实现相应抽象接口，并在 `backend/autoquant_backend/engine.py` 的工厂函数中注册即可。前端不得导入 `autoquant_backend`，后端不得导入 `autoquant_frontend`；跨端协议只放在 `autoquant_shared`。

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

导入结果可以保存到 `%LOCALAPPDATA%\AutoQuant\external_trade_experiences.json`，也可以使用“运行配置”页中填写的 OpenAI API Key 上传到新的或已有的 Vector Store。上传是显式操作，API Key 不会写入经验文件。若远程后端已有保存的 Key，读取配置时界面只会收到掩码；执行前端直传时需重新填写真实 Key。为避免混入旧的本地订单数据，这个外部经验库使用独立文件名；DeepSeek 后续可由本地检索器选取相关经验后随决策请求一起发送。

## 测试

测试不会连接 Binance，也不会下单：

```powershell
py -m unittest discover -s tests -v
```
