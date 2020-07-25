###
#
# Pull new image from Github
# docker pull docker.pkg.github.com/angellusmortis/media-processing/image:latest
#
# Create new image in Unraid
# docker create --cpus=12 -e CPU_THREADS=12 -v /mnt/user/media/music:/music -v /mnt/user/media/movies:/movies -v /mnt/user/processing/movies:/processing/movies -v /boot/config/ssh/:/ssh -v /mnt/user/download/:/download --name MediaProcessing docker.pkg.github.com/angellusmortis/media-processing/image:latest
#
# Run image
# docker run --cpus=12 -e CPU_THREADS=12 -v /mnt/user/media/music:/music -v /mnt/user/media/movies:/movies -v /mnt/user/processing/movies:/processing/movies -v /boot/config/ssh/:/ssh -v /mnt/user/download/:/download docker.pkg.github.com/angellusmortis/media-processing/image:latest
#
# Build image
#
# docker build -t docker.pkg.github.com/angellusmortis/media-processing/image:latest -f Dockerfile .
#
###

FROM linuxserver/ffmpeg

ENV CPU_THREADS 8

RUN \
 echo "**** install runtime ****" && \
 apt-get update && \
 apt-get install -y python3 python3-pip cron rsync openssh-client && \
 echo "**** clean up ****" && \
 rm -rf \
    /var/lib/apt/lists/* \
    /var/tmp/*

RUN pip3 install ffmpeg-python click click-pacbar gevent tendo psutil

RUN mkdir /processing /ssh /movies /music /download /root/.ssh

COPY ./process_media.py /
COPY ./known_hosts /root/.ssh/known_hosts
RUN chmod 0600 /root/.ssh/known_hosts
RUN chmod 0700 /root/.ssh/

COPY ./entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

COPY ./music /etc/cron.daily/
# RUN chmod 0755 /etc/cron.daily/music

COPY ./movies /etc/cron.hourly/
# RUN chmod 0755 /etc/cron.hourly/movies

COPY ./rsync /etc/cron.d/rsync

ENTRYPOINT ["/entrypoint.sh"]
CMD ["cron", "-f"]
