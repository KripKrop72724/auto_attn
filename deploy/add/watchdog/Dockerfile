FROM docker:28-cli

COPY deploy/add/watchdog/self_heal.sh /usr/local/bin/add-self-heal
RUN chmod 0555 /usr/local/bin/add-self-heal

HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD test -f /tmp/watchdog-heartbeat && test "$(( $(date +%s) - $(stat -c %Y /tmp/watchdog-heartbeat) ))" -lt 60

ENTRYPOINT ["/usr/local/bin/add-self-heal"]
