FROM linuxserver/ffmpeg

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
COPY ./music /etc/cron.daily/
COPY ./movies /etc/cron.hourly/

RUN chmod 0744 /etc/cron.daily/music
RUN chmod 0744 /etc/cron.hourly/movies

ENTRYPOINT []
CMD ["cron", "-f"]
