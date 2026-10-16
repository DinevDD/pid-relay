# Management UI. Based on the httpd image so that validation runs on exactly
# the same Apache build as the redirector.
FROM httpd:2.4
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
COPY ui/requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt
COPY pidrelay /app/pidrelay
COPY redirector /app/redirector
COPY ui /app/ui
RUN useradd --system --create-home --uid 10001 pidrelay \
 && mkdir -p /data /srv/deploy && chown pidrelay /data /srv/deploy
USER pidrelay
WORKDIR /app
ENV PATH=/opt/venv/bin:$PATH \
    PIDRELAY_HTTPD=/usr/local/apache2/bin/httpd \
    PIDRELAY_MODULES=/usr/local/apache2/modules \
    PIDRELAY_DATA=/data \
    PIDRELAY_DEPLOY=/srv/deploy \
    PIDRELAY_REDIRECTOR_URL=http://redirector \
    PYTHONUNBUFFERED=1
EXPOSE 8000
# One worker process (it owns the sync loop), several threads.
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:8000", "--access-logfile", "-", "ui.wsgi:application"]
