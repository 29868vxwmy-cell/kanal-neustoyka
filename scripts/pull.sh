#!/bin/sh
# Забирает новые черновики из GitHub; если поменялся код бота — перезапускает его.
set -eu
cd /opt/kanal-neustoyka
OLD=$(git rev-parse HEAD)
git pull --ff-only -q
if ! git diff --quiet "$OLD" HEAD -- bot deploy; then
	systemctl restart kanal-bot
fi
