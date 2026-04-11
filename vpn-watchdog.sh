#!/bin/bash
# VPN Watchdog — checks internet, reconnects NordVPN to Austria if dead
# Runs every 5 min via cron

LOG=/var/log/vpn-watchdog.log

# Quick connectivity check (3 pings, 2s timeout)
if ping -c 3 -W 2 8.8.8.8 > /dev/null 2>&1; then
    exit 0  # Internet works, nothing to do
fi

echo "[$(date -Is)] Internet down — reconnecting NordVPN to Austria..." >> "$LOG"

nordvpn disconnect >> "$LOG" 2>&1
sleep 3
nordvpn connect Austria >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
    echo "[$(date -Is)] nordvpn connect FAILED — NOT restarting bot" >> "$LOG"
    exit 1
fi
sleep 5

# Verify
if ping -c 3 -W 2 8.8.8.8 > /dev/null 2>&1; then
    echo "[$(date -Is)] VPN reconnected successfully" >> "$LOG"
    systemctl restart copybot
    echo "[$(date -Is)] copybot restarted" >> "$LOG"
else
    echo "[$(date -Is)] STILL NO INTERNET after reconnect — manual check needed!" >> "$LOG"
fi
