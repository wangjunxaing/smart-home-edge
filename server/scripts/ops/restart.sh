#!/bin/bash
tmux kill-session -t flask 2>/dev/null
tmux new-session -d -s flask 'cd /home/ubuntu/server && /home/ubuntu/ai_env/bin/python3 app.py'
sleep 3
curl -s http://localhost:8080/api/esp32/sensors/latest | python3 -c "import sys,json; d=json.load(sys.stdin); print('avg_lux=' + str(d['avg_lux']))"