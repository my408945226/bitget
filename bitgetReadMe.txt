cd /home/bitget && git pull

nohup python3 /home/bitget/monitor.py > /home/bitget/monitor.log 2>&1 &
tail -f /home/bitget/monitor.log
tail -f /home/bitget/strategy.log


scp C:\Users\Deedee\Desktop\database\bitget\.env root@47.250.154.50:/home/bitget/
scp -r root@47.250.154.50:/home/bitget/logs/REDUSDT_2026-07-05_11-52-44.log   C:\Users\Deedee\Desktop\logs\

--limit
--adopt <px>   指定基准价
--adopt        裸写：自动取该 symbol 账户最后成交价作基准（无成交则用当前市价）
nohup python3 strategy.py --symbol BGBUSDT --sz 10 --grid 0.005 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol IDUSDT --sz 1000 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol GENIUSUSDT --sz 100 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol TNSRUSDT --sz 1000 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol GUSDT --sz 20000 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol FUTUUSDT --sz 1 --grid 0.01 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol TOSHIUSDT --sz 500000 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol TACUSDT --sz 1000 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol FLUIDUSDT --sz 30 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol RPLUSDT --sz 50 --grid 0.02 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol SLPUSDT --sz 100000 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol RPLUSDT --sz 20 --grid 0.02 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol IOUSDT --sz 200 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol REDUSDT --sz 500 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol BANKUSDT --sz 1000 --grid 0.02 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol ARPAUSDT --sz 3000 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol FFUSDT --sz 3000 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol PARTIUSDT --sz 1000 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol MIRAUSDT --sz 1000 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol VELODROMEUSDT --sz 2000 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol POWRUSDT --sz 1000 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol LISTAUSDT --sz 1000 --grid 0.02 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol PHAUSDT --sz 2000 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol LUMIAUSDT --sz 500 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol AWEUSDT --sz 1000 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol NILUSDT --sz 1000 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol DEXEUSDT --sz 1 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol IMXUSDT --sz 500 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol HOTUSDT --sz 100000 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol OPENUSDT --sz 200 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 strategy.py --symbol SYNUSDT --sz 100 --grid 0.025 >> strategy.log 2>&1 &



删除
nohup python3 strategy.py --symbol SXTUSDT --sz 5000 --grid 0.025 >> strategy.log 2>&1 &











