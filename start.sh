#!/bin/bash

for f in $(find /etc/cron.d/ -type f); do
    echo "Found crontab: $f"
    chmod 644 "$f"
    crontab "$f"
done

echo "Starting cron..."
cron -f -l 15
