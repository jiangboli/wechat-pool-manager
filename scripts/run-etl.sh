#!/bin/bash
# Claw DO Analytics ETL - daily data collection
docker cp /home/dosh/wechat-pool-manager/scripts/etl-analytics.py pool-admin:/tmp/
docker exec pool-admin python3 /tmp/etl-analytics.py 2>&1 | logger -t claw-do-etl
