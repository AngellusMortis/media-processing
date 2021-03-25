###
#
# Pull new image from Github
# docker pull docker.pkg.github.com/angellusmortis/media-processing/image:latest
#
# Create new image in Unraid
# docker create --cpus=20 -e CPU_THREADS=20 -v /mnt/user/media/music:/music -v /mnt/user/media/movies:/movies -v /mnt/user/processing/movies:/processing/movies -v /boot/config/ssh/:/ssh -v /mnt/user/download/:/download -v /mnt/user/backup/:/backup -v /mnt/user/media/security:/security --name MediaProcessing docker.pkg.github.com/angellusmortis/media-processing/image:latest
#
# Run image
# docker run --cpus=20 -e CPU_THREADS=20 -v /mnt/user/media/music:/music -v /mnt/user/media/movies:/movies -v /mnt/user/processing/movies:/processing/movies -v /boot/config/ssh/:/ssh -v /mnt/user/download/:/download -v /mnt/user/backup/:/backup -v /mnt/user/media/security:/security docker.pkg.github.com/angellusmortis/media-processing/image:latest
#
# Build image
#
# docker build -t docker.pkg.github.com/angellusmortis/media-processing/image:latest -f Dockerfile .
#
###

FROM linuxserver/ffmpeg

ENV CPU_THREADS 8
ENV RUN_GROUP rsync

RUN \
 echo "**** install runtime ****" && \
 apt-get update && \
 apt-get install -y python3 python3-pip cron rsync openssh-client expect && \
 echo "**** clean up ****" && \
 rm -rf \
    /var/lib/apt/lists/* \
    /var/tmp/*

RUN pip3 install ffmpeg-python click click-pacbar gevent tendo psutil

RUN cd /tmp/ && curl -sLo hdr10plus_parser.tar.gz https://github.com/quietvoid/hdr10plus_parser/releases/download/0.3.1/hdr10plus_parser-x86_64-unknown-linux-musl.tar.gz && \
 tar -xf hdr10plus_parser.tar.gz && \
 mv dist/hdr10plus_parser /usr/local/bin && \
 rm hdr10plus_parser.tar.gz dist -rf

RUN mkdir /processing /ssh /movies /music /download /root/.ssh

COPY ./process_media.py /usr/local/bin/process_media
RUN chmod +x /usr/local/bin/process_media

COPY ./known_hosts /root/.ssh/known_hosts
RUN chmod 0600 /root/.ssh/known_hosts
RUN chmod 0700 /root/.ssh/

COPY ./entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

RUN mkdir -p /cron
COPY ./music ./movies ./rsync /cron/
RUN find /cron -type f | xargs chmod 0755;

ENTRYPOINT ["/entrypoint.sh"]

# COPY ./music /etc/cron.daily/
# COPY ./movies /etc/cron.hourly/
# COPY ./rsync /etc/cron.d/rsync
CMD ["cron", "-f"]
