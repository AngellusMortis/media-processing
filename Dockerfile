FROM linuxserver/ffmpeg

ENV CPU_THREADS 8
ENV RUN_GROUP rsync

RUN \
 echo "**** install runtime ****" && \
 apt-get update && \
 apt-get install -y sshpass python3 python3-pip cron rsync openssh-client expect && \
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

COPY ./start.sh /usr/local/bin/start
RUN chmod +x /usr/local/bin/start

ENTRYPOINT []
CMD ["start"]
