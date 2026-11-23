# victor-voip — your own SIP server (Asterisk/PJSIP)

```sh
sudo apt install asterisk openssl            # Debian/Ubuntu (Arch: asterisk from AUR/extra)
export VICTOR_VOIP_DIR=~/.local/share/victor-voip
./victor_voip.py init --host sip.example.org           # TLS 5061, self-signed cert; prints the fingerprint phones pin
./victor_voip.py add-device pixel --pstn               # prints provisioning JSON ONCE (extension + 24-char password)
VOIP_TRUNK_PASSWORD=... ./victor_voip.py trunk --host sip.provider.example --user ACCOUNT   # optional PSTN trunk
./victor_voip.py set --allow-prefix +1 --allow-prefix +44 --max-pstn 2 --inbound-ext 1001
./victor_voip.py run --foreground
```
Open **TCP 5061** and **UDP 10000-10200** (RTP) to the server only; there is no plaintext SIP listener. Put fail2ban on `log/full` ("Failed to authenticate") if exposed to the internet.

Security: TLS 1.2+, SRTP (SDES, required), no guest calls, per-phone credentials, PSTN only for `--pstn` devices, destination prefix allow-list and a concurrent-PSTN-call cap (toll-fraud limits). Secrets: `secrets.json` (0600).

Verified here (`python3 -m unittest tests.test_voip`, real Asterisk 20.6 over TLS): digest registration OK, wrong password / unknown user / anonymous refused, no UDP listener, config renders and loads. **Not verified:** actual calls, SRTP media, NAT traversal, any PSTN trunk (needs a provider account and credentials from you).
Caveat: SDES keys travel in SDP, so TLS to the server is what protects them; use a trusted/pinned server certificate.
