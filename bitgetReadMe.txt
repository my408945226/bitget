cd /home/bitget && git pull

nohup python3 /home/bitget/monitor.py > /home/bitget/monitor.log 2>&1 &

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

--initial-sell-px
--adopt-sell-px


cd C:\Users\Deedee\Desktop\database\bitget
python3 -m bitget_short_pyramid.strategy live --symbol BGBUSDT --size 4 --grid 0.005 --adopt-sell-px 1.7815

python -m bitget_short_pyramid.strategy live --symbol IDUSDT --size 200 --grid 0.01