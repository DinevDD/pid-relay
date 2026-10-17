# Management UI. Based on the httpd image so that validation runs on exactly
# the same Apache build as the redirector.

# w3id.org's own contribution checker (tools/check), built with Node 22.
FROM node:22-slim AS w3id
ARG W3ID_REF=master
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
 && git clone --quiet --depth 1 --branch "$W3ID_REF" --filter=blob:none --sparse \
      https://github.com/perma-id/w3id.org.git /opt/w3id \
 && git -C /opt/w3id sparse-checkout set --no-cone /tools/check/ /.w3id-check.yaml \
 && cd /opt/w3id/tools/check && npm ci --omit=dev --no-audit --no-fund \
 && rm -rf /opt/w3id/.git

FROM httpd:2.4
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates libstdc++6 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=w3id /usr/local/bin/node /usr/local/bin/node
COPY --from=w3id /opt/w3id /opt/w3id
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
    PIDRELAY_W3ID_CHECK=/opt/w3id/tools/check/bin/w3id-check.js \
    PYTHONUNBUFFERED=1
EXPOSE 8000
# One worker process (it owns the sync loop), several threads.
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:8000", "--access-logfile", "-", "ui.wsgi:application"]
