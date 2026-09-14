FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y openssh-server ca-certificates bash coreutils findutils tar util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash harness \
    && echo 'harness:harness' | chpasswd \
    && mkdir -p /run/sshd /opt/harness/bin \
    && printf '%s\n' \
      '#!/bin/sh' \
      'set -eu' \
      'output=' \
      'sleep_seconds=0' \
      'while [ "$#" -gt 0 ]; do' \
      '  case "$1" in' \
      '    --output) output="$2"; shift 2 ;;' \
      '    --sleep) sleep_seconds="$2"; shift 2 ;;' \
      '    *) shift ;;' \
      '  esac' \
      'done' \
      'trap "exit 143" TERM INT' \
      'if [ "$sleep_seconds" -gt 0 ]; then sleep "$sleep_seconds"; fi' \
      'if [ -n "$output" ]; then printf "fake codex completed\\n" > "$output"; fi' \
      'printf "fake codex completed\\n"' \
      > /opt/harness/bin/fake-codex \
    && chmod 0755 /opt/harness/bin/fake-codex \
    && printf '%s\n' \
      'PermitRootLogin no' \
      'PasswordAuthentication no' \
      'KbdInteractiveAuthentication no' \
      'PubkeyAuthentication yes' \
      'UsePAM no' \
      'PrintMotd no' \
      'StrictModes yes' \
      'AllowUsers harness' \
      'KexAlgorithms curve25519-sha256,diffie-hellman-group14-sha256' \
      > /etc/ssh/sshd_config.d/harness.conf

COPY ssh-entrypoint.sh /usr/local/bin/ssh-entrypoint
RUN sed -i 's/\r$//' /usr/local/bin/ssh-entrypoint && chmod 0755 /usr/local/bin/ssh-entrypoint

EXPOSE 22
ENTRYPOINT ["/usr/local/bin/ssh-entrypoint"]
