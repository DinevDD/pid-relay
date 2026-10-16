# The redirect service: stock Apache httpd with the pid-relay configuration.
FROM httpd:2.4
COPY redirector/pid-relay.conf /etc/pid-relay/pid-relay.conf
RUN mkdir -p /usr/local/apache2/empty /srv/deploy && chown 10001 /srv/deploy
ENV PIDRELAY_MODULES=/usr/local/apache2/modules \
    PIDRELAY_RULES=/srv/deploy/rules \
    PIDRELAY_LISTEN=80 \
    PIDRELAY_RUNDIR=/usr/local/apache2 \
    PIDRELAY_ERRORLOG=/proc/self/fd/2 \
    PIDRELAY_ACCESSLOG=/proc/self/fd/1
EXPOSE 80
CMD ["httpd", "-f", "/etc/pid-relay/pid-relay.conf", "-DFOREGROUND"]
