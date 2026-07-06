FROM alpine:3.20
RUN apk add --no-cache curl
COPY watchdog.sh /watchdog.sh
RUN chmod +x /watchdog.sh
CMD ["/watchdog.sh"]
