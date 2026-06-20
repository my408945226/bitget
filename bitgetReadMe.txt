cd /home/bitget && git pull

nohup python3 /home/bitget/monitor.py > /home/bitget/monitor.log 2>&1 &
tail -f /home/bitget/monitor.log

scp C:\Users\Deedee\Desktop\database\bitget\.env root@43.135.14.231:/home/bitget/
scp -r root@43.135.14.231:/home/bitget/logs/  C:\Users\Deedee\Desktop\logs\

--initial-sell-px
--adopt-sell-px


nohup python3 -m strategy --symbol BGBUSDT --sz 10 --grid 0.005 >> strategy.log 2>&1 &
nohup python3 -m strategy --symbol IDUSDT --sz 200 --grid 0.025 >> strategy.log 2>&1 &
nohup python3 -m strategy --symbol GENIUSUSDT --sz 100 --grid 0.025 >> strategy.log 2>&1 &





