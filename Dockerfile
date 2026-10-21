FROM python@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 2000 watchkeeper-sockets \
 && useradd --uid 1000 --gid 2000 --no-create-home --home-dir /var/empty --shell /usr/sbin/nologin watchkeeper \
 && install -d -o 1000 -g 2000 -m 0700 /var/lib/watchkeeper /var/lib/watchkeeper/email /var/lib/watchkeeper/smtp \
 && install -d -o 1000 -g 2000 -m 0750 /run/watchkeeper/broker /run/watchkeeper/proxy
WORKDIR /opt/watchkeeper
COPY --chown=1000:2000 watchkeeper /opt/watchkeeper/watchkeeper
USER 1000:2000
EXPOSE 8765
ENTRYPOINT ["python", "-m", "watchkeeper"]
