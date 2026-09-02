FROM docker:28-cli

COPY deploy/add/watchdog/self_heal.sh /usr/local/bin/add-self-heal
# Git on a Windows production runner may materialize shell scripts with CRLF.
# Normalize defensively in the Linux image even though .gitattributes pins LF.
RUN sed -i 's/\r$//' /usr/local/bin/add-self-heal \
    && chmod 0555 /usr/local/bin/add-self-heal

HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD test -f /tmp/watchdog-heartbeat && test "$(( $(date +%s) - $(stat -c %Y /tmp/watchdog-heartbeat) ))" -lt 60

ENTRYPOINT ["/usr/local/bin/add-self-heal"]
