#!/bin/bash

echo CPU_THREADS=${CPU_THREADS} >> /etc/environment

if [ "$RUN_GROUP" == "music" ]; then
    cp /cron/music /etc/cron.daily/music
    chmod 0755 /etc/cron.daily/music
elif [ "$RUN_GROUP" == "movies" ]; then
    cp /cron/movies /etc/cron.hourly/movies
    chmod 0755 /etc/cron.hourly/movies
elif [ "$RUN_GROUP" == "rsync" ]; then
    cp /cron/rsync /etc/cron.d/rsync
    chmod 0755 /etc/cron.d/rsync
else
    echo "Unknown RUN_GROUP"
    exit 1
fi

exec $@
