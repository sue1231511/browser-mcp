#!/bin/sh
 
# 启动虚拟显示器
Xvfb :99 -screen 0 1280x900x24 &
export DISPLAY=:99
sleep 2
 
# 启动 VNC 服务
x11vnc -display :99 -nopw -forever -shared -rfbport 5900 -quality 100 &
sleep 2
 
# 启动 noVNC
websockify --web=/usr/share/novnc 6080 localhost:5900 &
sleep 1
 
# 启动 nginx
nginx &
 
# 启动 MCP 服务
PORT=8081 python main.py
 
