FROM docker.io/library/python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/nonexistent
WORKDIR /app
COPY printbench/*.py ./printbench/
COPY web/ ./web/
USER 10001:10001
STOPSIGNAL SIGINT
# The mounted filesystem root can contain lost+found, which is not a project.
CMD ["sh", "-ec", "mkdir -p -m 0700 /data/library; test ! -L /data/library; test -O /data/library; test -w /data/library; test -x /data/library; exec python -m printbench --data /data/library serve"]
