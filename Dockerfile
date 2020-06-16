###
# docker pull docker.pkg.github.com/angellusmortis/media-processing/image:latest
# docker create --cpus=12 -e CPUS_THREADS=12 -v /mnt/user/media/music:/music -v /mnt/user/media/movies:/movies -v /mnt/user/processing/movies:/processing --name Media docker.pkg.github.com/angellusmortis/media-processing/image:latest
###

FROM linuxserver/ffmpeg

ENV CPU_THREADS 8

RUN \
 echo "**** install runtime ****" && \
 apt-get update && \
 apt-get install -y python3 python3-pip cron && \
 echo "**** clean up ****" && \
 rm -rf \
    /var/lib/apt/lists/* \
    /var/tmp/*

RUN pip3 install ffmpeg-python click click-pacbar gevent tendo psutil

COPY ./process_media.py /

COPY ./entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

COPY ./music /etc/cron.daily/
RUN chmod 0755 /etc/cron.daily/music

COPY ./movies /etc/cron.hourly/
RUN chmod 0755 /etc/cron.hourly/movies

ENTRYPOINT ["/entrypoint.sh"]
CMD ["cron", "-f"]

