# Wi-Fi hotspot (optional, off by default)

Lets a phone reach this PC's API with no router: the PC's Wi-Fi adapter becomes an access point.

* **Off by default.** Nothing starts at boot. It runs only after you press Start (Control Center > Hotspot, or `victor-ctl hotspot start` on the Debian edition) and is gone after a reboot. The NetworkManager profile is created with `autoconnect no`.
* **Isolated.** Joined devices get an address on 10.77.0.0/24 and can reach only this PC (API key still required). No internet sharing: no NAT, no forwarding (`ap_isolate=1`; on Debian the firewall drops all forwarding to and from the hotspot). Phones keep their own mobile data for the internet.
* **Admin UI stays local.** The Control Center still listens on 127.0.0.1 only. The hotspot is useful only when Server & Access (remote API) is on and bound to 0.0.0.0.
* **Password.** WPA2-PSK (CCMP). A random 16-character password is generated and stored with mode 0600 on the data partition. It is shown only when you press Show / Join QR (standard `WIFI:` QR code). On the Debian CLI it is never accepted on the command line.
* **5 GHz** needs your two-letter country code and uses non-DFS channels 36/40/44/48 only.

| Edition | How |
|---|---|
| Arch | `victor-priv hotspot-start` writes hostapd + DHCP-only dnsmasq configs to `/run/victorlinux/hotspot` (0600) and runs them as transient systemd units. Packages: hostapd, dnsmasq, iw, wireless-regdb. |
| Debian | NetworkManager AP profile (`victor-hotspot`); `victor-priv hotspot-fw` adds the interface to the nftables `hotspot_if` set (DHCP/DNS in, all forwarding dropped). |
| Pi | Not wired yet (the Pi edition uses NetworkManager too, so the Debian path should port cheaply). |

Not every adapter supports AP mode; the Control Center warns when `iw list` does not show it. Nothing here has been tried on real Wi-Fi hardware.
