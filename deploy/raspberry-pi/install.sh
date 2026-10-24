#!/usr/bin/env bash
# Install a Google Play build watcher on a Raspberry Pi (or any systemd Linux).
# It only LOGS which builds Play offers and when (no downloads):
#   <DIR>/data/<package>.observations.csv   every observation (time, source, version, version code)
#   <DIR>/data/<package>.state.json         every build ever seen + first/last seen
#   mobile-store-scraper report <package> --state-dir <DIR>/data
#
# Usage (defaults shown):
#   PACKAGE=com.naturalmotion.customstreetracer2 INTERVAL='*:0/30' SAMPLES=6 COUNTRY=DE REF=main \
#     bash install.sh
# Run it again with another PACKAGE to watch more apps (one systemd timer per app).
set -euo pipefail

PACKAGE="${PACKAGE:-com.naturalmotion.customstreetracer2}"
INTERVAL="${INTERVAL:-*:0/30}"        # systemd OnCalendar; '*:0/30' = every 30 min, 'hourly' = hourly
SAMPLES="${SAMPLES:-6}"               # fresh device check-ins per run (0 = only the main session)
COUNTRY="${COUNTRY:-DE}"
REF="${REF:-main}"                    # git branch/tag to install from
DIR="${DIR:-$HOME/store-watch}"
RUN_USER="${RUN_USER:-$USER}"

python3 - <<'PY' || { echo "Python >= 3.10 needed (Raspberry Pi OS Bookworm or newer)"; exit 1; }
import sys; assert sys.version_info >= (3, 10)
PY
if ! python3 -m venv --help >/dev/null 2>&1 || ! command -v git >/dev/null; then
  sudo apt-get update && sudo apt-get install -y python3-venv git
fi

mkdir -p "$DIR/data"
[ -x "$DIR/venv/bin/python" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q --upgrade "mobile-store-scraper @ git+https://github.com/Nitro4CSR/mobile-store-scraper@${REF}"

# per-app settings
cat > "$DIR/$PACKAGE.env" <<ENV
COUNTRY=$COUNTRY
SAMPLES=$SAMPLES
ENV

sudo tee /etc/systemd/system/mss-watch@.service >/dev/null <<UNIT
[Unit]
Description=mobile-store-scraper build watch for %i
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
EnvironmentFile=$DIR/%i.env
Environment=XDG_CACHE_HOME=$DIR/cache
ExecStart=$DIR/venv/bin/mobile-store-scraper watch %i --state-dir $DIR/data --country \${COUNTRY} --samples \${SAMPLES} --always-sample
TimeoutStartSec=600
Nice=10
UNIT

sudo tee "/etc/systemd/system/mss-watch@$PACKAGE.timer" >/dev/null <<UNIT
[Unit]
Description=Run mobile-store-scraper build watch for $PACKAGE ($INTERVAL)

[Timer]
OnCalendar=$INTERVAL
RandomizedDelaySec=120
Persistent=true
Unit=mss-watch@$PACKAGE.service

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now "mss-watch@$PACKAGE.timer"
echo "First check running now..."
sudo systemctl start "mss-watch@$PACKAGE.service" || true

cat <<DONE

Installed. Watching $PACKAGE ($COUNTRY) on schedule '$INTERVAL', $SAMPLES samples per run.
  Builds seen so far : $DIR/venv/bin/mobile-store-scraper report $PACKAGE --state-dir $DIR/data
  Every observation  : $DIR/data/$PACKAGE.observations.csv
  Run log            : journalctl -u mss-watch@$PACKAGE -n 50
  Next runs          : systemctl list-timers 'mss-watch@*'
  Remove             : sudo systemctl disable --now mss-watch@$PACKAGE.timer
DONE
sudo -u "$RUN_USER" "$DIR/venv/bin/mobile-store-scraper" report "$PACKAGE" --state-dir "$DIR/data" || true
