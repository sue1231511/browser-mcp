#!/bin/sh
set -e
 
# 启动虚拟显示器
Xvfb :99 -screen 0 1280x900x24 &
export DISPLAY=:99
sleep 1
 
# 启动 VNC 服务（最高画质，无密码，永久运行）
x11vnc -display :99 -nopw -forever -shared -rfbport 5900 -quality 100 -noxdamage &
sleep 1
 
# 启动 noVNC（websockify 把 VNC 桥接到 WebSocket，同时提供 Web 界面）
websockify --web=/usr/share/novnc 6080 localhost:5900 &
sleep 1
 
# 启动 nginx
nginx &
 
# 启动 MCP 服务（内部端口 8081，对外由 nginx /mcp 路由）
PORT=8081 python main.py
 
