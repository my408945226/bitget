cd /home/bitget && git pull

nohup python3 /home/bitget/monitor.py > /home/bitget/monitor.log 2>&1 &
tail -f /home/bitget/monitor.log

scp -r root@43.135.14.231:/home/bitget/logs/  C:\Users\Deedee\Desktop\logs\

--initial-sell-px
--adopt-sell-px


cd C:\Users\Deedee\Desktop\database\bitget
python3 -m strategy --symbol BGBUSDT --sz 4 --grid 0.005 --adopt-sell-px 1.806

python3 -m strategy --symbol IDUSDT --sz 200 --grid 0.01



