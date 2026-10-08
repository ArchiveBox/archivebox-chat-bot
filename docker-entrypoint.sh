#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
    chown bridge:bridge /data
    exec gosu bridge "$@"
fi

exec "$@"
