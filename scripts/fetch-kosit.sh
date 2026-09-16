#!/bin/sh
# Downloads the official KoSIT validator and XRechnung rules into vendor/kosit (not committed).
set -eu
DEST="${1:-$(dirname "$0")/../vendor/kosit}"
VALIDATOR=1.6.3
CONFIG=2026-08-31
XR=3.0.2
mkdir -p "$DEST"
cd "$DEST"
[ -f validator.jar ] || curl -fsSL -o validator.jar \
  "https://github.com/itplr-kosit/validator/releases/download/v$VALIDATOR/validator-$VALIDATOR-standalone.jar"
if [ ! -f config/scenarios.xml ]; then
  curl -fsSL -o config.zip \
    "https://github.com/itplr-kosit/validator-configuration-xrechnung/releases/download/v$CONFIG/xrechnung-$XR-validator-configuration-$CONFIG.zip"
  rm -rf config && mkdir config && unzip -q config.zip -d config && rm config.zip
fi
echo "KoSIT ready in $DEST"
