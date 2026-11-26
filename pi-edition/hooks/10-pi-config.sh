#!/bin/bash
# Pi edition system configuration, run inside the arm64 rootfs after packages and the overlay are in place.
set -Eeuo pipefail
. /usr/share/victorlinux/versions.env
. /usr/share/victorlinux/pi-build.env
export DEBIAN_FRONTEND=noninteractive

# --- owner account: locked (no shared default password). First-boot setup sets the owner's own password.
getent group victor-admin >/dev/null || groupadd -r victor-admin
id victor >/dev/null 2>&1 || useradd -m -u 1000 -s /bin/bash -c "VictorLinux owner" victor
passwd -l victor >/dev/null
install -d -m 0750 -o victor -g victor /home/victor
for g in audio video render plugdev netdev users dialout bluetooth victor-admin; do
  getent group "$g" >/dev/null || { echo "required group $g missing" >&2; exit 1; }
  usermod -aG "$g" victor
done
for g in adbusers gpio i2c spi input; do getent group "$g" >/dev/null && usermod -aG "$g" victor || true; done

# --- identity
echo "victorlinux-pi" > /etc/hostname
rm -f /etc/os-release
cat > /etc/os-release <<'OS'
NAME="VictorLinux"
PRETTY_NAME="VictorLinux (Raspberry Pi edition)"
ID=victorlinux
ID_LIKE=debian
VERSION_ID="13"
VERSION_CODENAME=trixie
VARIANT_ID=pi
HOME_URL="https://github.com/Victorious93/VictorLinux"
OS
printf 'VictorLinux (Raspberry Pi edition) \\l\n\n' > /etc/issue
cat > /etc/hosts <<'H'
127.0.0.1 localhost
127.0.1.1 victorlinux-pi
::1 localhost ip6-localhost ip6-loopback
H

# --- locale / keyboard: English only
sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
locale-gen >/dev/null
printf 'LANG=en_US.UTF-8\n' > /etc/default/locale
printf 'XKBMODEL="pc105"\nXKBLAYOUT="us"\nXKBVARIANT=""\nXKBOPTIONS=""\nBACKSPACE="guess"\n' > /etc/default/keyboard

# --- Bluetooth: bluetoothd available, adapter NOT powered automatically (off by default; victor-pi bluetooth on)
if [ -f /etc/bluetooth/main.conf ]; then
  if grep -qE '^#?AutoEnable' /etc/bluetooth/main.conf; then
    sed -i -E 's/^#?AutoEnable *=.*/AutoEnable=false/' /etc/bluetooth/main.conf
  else
    printf '\n[Policy]\nAutoEnable=false\n' >> /etc/bluetooth/main.conf
  fi
fi

# --- services
systemctl disable ssh.service ssh.socket 2>/dev/null || true
# Raspberry Pi conveniences that would bypass VictorLinux policy: SSH-on-by-file, root resize, silent EEPROM flashing
for u in sshswitch.service rpi-resize.service rpi-eeprom-update.service userconfig.service \
         apt-daily.timer apt-daily-upgrade.timer man-db.timer e2scrub_all.timer; do
  systemctl mask "$u" 2>/dev/null || true
done
systemctl disable NetworkManager-wait-online.service 2>/dev/null || true
systemctl enable NetworkManager.service bluetooth.service nftables.service systemd-timesyncd.service 2>/dev/null || true
systemctl enable victor-storage.service victor-access.service victor-pi-board.service victor-pi-grow-data.service \
  victor-pi-firstboot-file.service victor-pi-keys.service victor-owner-console.service
systemctl enable hciuart.service 2>/dev/null || true   # present only in some pi-bluetooth versions
rm -f /etc/systemd/system/*.wants/victor-llama.service

if [ "$PI_DESKTOP" = 1 ]; then
  systemctl set-default graphical.target
  install -d /etc/lightdm/lightdm.conf.d
  printf '[Seat:*]\nautologin-user=victor\nautologin-user-timeout=0\nuser-session=xfce\n' > /etc/lightdm/lightdm.conf.d/50-victorlinux.conf
  # the Pi setup screen replaces the Debian edition's password-only first-run dialog
  rm -f /etc/skel/.config/autostart/victor-firstrun.desktop /home/victor/.config/autostart/victor-firstrun.desktop
  systemctl disable victor-pi-setup-tty.service 2>/dev/null || true
else
  systemctl set-default multi-user.target
  systemctl enable victor-pi-setup-tty.service
  # the headless wizard asks for the first owner password itself (no skip), so the console service only approves drives
  install -d /etc/systemd/system/victor-owner-console.service.d
  printf '[Service]\nExecStart=\nExecStart=/usr/local/lib/victorlinux/victor-owner-setup approve --timeout 120\n' \
    > /etc/systemd/system/victor-owner-console.service.d/headless.conf
  rm -f /usr/local/bin/victor-pi-setup /usr/local/bin/victor-control /usr/local/bin/victor-firstrun /usr/local/bin/victor-theme
fi
# skel -> owner home (the account was created before the overlay in some orders; keep it in sync)
cp -a /etc/skel/. /home/victor/ && chown -R victor:victor /home/victor

# --- sudoers: only the two validated helpers are passwordless
chmod 0440 /etc/sudoers.d/victor-priv /etc/sudoers.d/victor-pi-priv
visudo -cf /etc/sudoers.d/victor-priv
visudo -cf /etc/sudoers.d/victor-pi-priv

# --- permissions of shipped tools
chmod 0755 /usr/local/bin/victor-* 2>/dev/null || true
find /usr/local/lib/victorlinux -maxdepth 1 -type f -exec chmod 0755 {} +
chmod 0750 /usr/local/lib/victorlinux/victor-priv /usr/local/lib/victorlinux/pi/victor-pi-priv
chmod 0700 /usr/local/lib/victorlinux/victor-owner-setup   # root-only console tool, not in sudoers
chmod 0755 /usr/local/lib/victorlinux/pi/victor-pi-grow-data /usr/local/lib/victorlinux/pi/hooks/run-hook
chmod 0644 /usr/local/lib/victorlinux/pi/victorpi.py

# --- SD-card friendly logging, no core dumps
install -d /etc/systemd/journald.conf.d /etc/sysctl.d
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=48M\n' > /etc/systemd/journald.conf.d/victor.conf
echo "kernel.core_pattern=|/bin/false" > /etc/sysctl.d/90-victor-nocore.conf

# --- per-device identities must not be baked in
: > /etc/machine-id
rm -f /var/lib/dbus/machine-id /etc/ssh/ssh_host_*

# --- facts for the manifest
{
  for k in linux-image-rpi-v8 linux-image-rpi-2712 raspi-firmware firmware-brcm80211 bluez bluez-firmware pi-bluetooth \
           network-manager adb scrcpy python3; do
    echo "pkg_${k}=$(dpkg-query -W -f='${Version}' "$k" 2>/dev/null || echo absent)"
  done
  ls /lib/modules | sed 's/^/kernel_modules_dir=/'
  echo "boot_files=$(cd /boot/firmware && ls | tr '\n' ' ')"
} > /usr/share/victorlinux/image-facts
dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > /usr/share/victorlinux/packages.tsv
