## 3. 运行 dry-run（不实际下单）

```bash
python -m bitget_short_pyramid.strategy dry-run \
  --symbol WLDUSDT --size 100 --grid 0.02 --multiplier 1.2 \
  --layers 8 --tp-pct 0.01 --leverage 3 --interval 5
```
python -m bitget_short_pyramid.strategy dry-run --symbol BGBUSDT --size 4 --grid 0.005

## 4. 运行 live（实盘下单）

```bash
python -m bitget_short_pyramid.strategy live \
  --symbol WLDUSDT --size 100 --grid 0.02 --multiplier 1.2 \
  --layers 8 --tp-pct 0.01 --leverage 3 --interval 5
```

> live 模式启动前会打印风险提示，需要输入 `yes` 确认。

## 5. 参数说明

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--symbol` | 交易对 | (必填) |
| `--size` | 首层开仓数量 | (必填) |
| `--grid` | 网格间距 (0.02=2%) | 0.02 |
| `--multiplier` | 每层递增倍数 | 1.2 |
| `--layers` | 最大层数 | 8 |
| `--tp-pct` | 止盈比例 (0.01=1%) | 0.01 |
| `--leverage` | 杠杆 | 3 |
| `--interval` | 轮询间隔(秒) | 5 |
| `--margin-mode` | 保证金模式 crossed/isolated | crossed |
| `--min-liq-dist` | 最小强平距离 | 0.15 |
| `--max-pos-usdt` | 最大持仓价值(USDT)，0=不限 | 0 |
| `--max-orders` | 每日最大下单次数 | 100 |


cd C:\Users\Deedee\Desktop\database\bitget
python -m bitget_short_pyramid.strategy live --symbol BGBUSDT --size 4 --grid 0.005
python -m bitget_short_pyramid.strategy live --symbol IDUSDT --size 200 --grid 0.01