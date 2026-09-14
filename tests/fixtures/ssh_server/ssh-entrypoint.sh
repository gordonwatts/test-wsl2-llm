#!/bin/sh
set -eu

: "${AUTHORIZED_KEY:?AUTHORIZED_KEY is required}"
mkdir -p /home/harness/.ssh /run/sshd
printf '%s\\n' "$AUTHORIZED_KEY" > /home/harness/.ssh/authorized_keys
chown -R harness:harness /home/harness/.ssh
chmod 700 /home/harness/.ssh
chmod 600 /home/harness/.ssh/authorized_keys
ssh-keygen -A >/dev/null
exec /usr/sbin/sshd -D -e
