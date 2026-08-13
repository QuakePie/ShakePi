# ShakePie OLED status display

`shakepie-oled` renders the recorder's status on a small SSD1306 OLED (128×32, I²C,
address `0x3C`) so anyone walking past the recorder can see that it is healthy.

```
*REC 200Hz 21:37
DISK 78% ~124d
OK 2E 47C
```

Three bold lines (DejaVu Sans Bold, 9 px — four thin lines are unreadable at this panel
size):

- **Line 1** — recording state, sample rate, local time; the leading dot blinks every
  second as a live heartbeat. Flips to `STOP` / `FAULT <code>` when the recorder is not
  running.
- **Line 2** — disk percent used and the estimated days until the disk is full (based on
  the recorder's actual daily volume). Above 90% it shows `DISK n% FULL SOON`.
- **Line 3** — health token (`OK`, `ADC`, `STOR`, `G12`, `STALE`, or `F:<fatal code>`),
  events detected today (`2E`), SoC temperature, and a `GPS` marker when a gpsd-reachable
  receiver has a fix (reserved for the planned GNSS module — without one the probe is
  silent).

The display dims automatically between 23:00 and 07:00 (local time) to prevent OLED
burn-in, and blanks itself cleanly when the service stops.

## Wiring (SSD1306 I²C module → Raspberry Pi header)

| OLED pin | Pi physical pin | Pi function |
| --- | --- | --- |
| VCC | pin 1 | 3V3 |
| GND | pin 6 | GND |
| SCL | pin 5 | GPIO3 (SCL) |
| SDA | pin 3 | GPIO2 (SDA) |

Enable I²C (`sudo raspi-config` → Interface Options → I²C) and verify:

```text
i2cdetect -y 1     # the display must appear at 0x3C
```

The GNSS module (when installed later) uses different pins and does not conflict:
module TX → GPIO15/RXD (pin 10), PPS → GPIO4 (pin 7), VCC/GND as usual.

## Install on the recorder Pi

```text
sudo mkdir -p /opt/shakepie/oled
sudo cp -a terminal-or-repo-checkout/oled/. /opt/shakepie/oled/
cd /opt/shakepie/oled
sudo python3 -m venv .venv
sudo .venv/bin/pip install .
sudo usermod -aG i2c shakepie          # display access for the service account
sudo cp deploy/shakepie-oled.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now shakepie-oled.service
```

Without a display connected the service waits and retries every 30 s, then starts
rendering as soon as the panel appears on I²C. For a no-hardware preview run
`shakepie-oled --simulate --frames 2`, which prints the four lines instead.
