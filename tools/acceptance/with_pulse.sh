#!/bin/sh
# Run a command against a private, throwaway PulseAudio with virtual sinks only (no hardware, no desktop audio).
# Usage: dbus-run-session -- tools/acceptance/with_pulse.sh <command...>     Work by Victorious.
set -u
export PULSE_RUNTIME_PATH="$(mktemp -d /tmp/vpulse.XXXX)"
cat > "$PULSE_RUNTIME_PATH/default.pa" <<PA
load-module module-native-protocol-unix socket=$PULSE_RUNTIME_PATH/native
load-module module-null-sink sink_name=vtest_sink rate=16000 channels=1
load-module module-remap-source master=vtest_sink.monitor source_name=vtest_mic channels=1 source_properties=device.description=Victor_test_mic
PA
pulseaudio -n -F "$PULSE_RUNTIME_PATH/default.pa" --exit-idle-time=-1 --daemonize=no --use-pid-file=no \
  --log-target=file:"$PULSE_RUNTIME_PATH/log" &
PAPID=$!
export PULSE_SERVER="unix:$PULSE_RUNTIME_PATH/native"
i=0; while [ $i -lt 10 ] && ! pactl info >/dev/null 2>&1; do sleep 1; i=$((i+1)); done
"$@"; rc=$?
kill $PAPID 2>/dev/null; rm -rf "$PULSE_RUNTIME_PATH"; exit $rc
