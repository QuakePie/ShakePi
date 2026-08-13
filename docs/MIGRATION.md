# Migration and rollback

1. Leave the legacy tree and service unchanged.
2. Build and test ShakePie with `adc.backend=fake` while writing to the real mounted HDD.
3. Verify `.spb` files with `shakepie-inspect`, deliberate client disconnects, live gaps, and `.partial` recovery.
4. Stop the legacy recorder before opening the real SPI/GPIO devices.
5. Confirm channel mapping, polarity, PGA headroom, group rate, and jitter with short hardware runs.
6. Use a new HDD subdirectory so old data is never overwritten.
7. Enable `seisberry-recorder.service` only after the hardware checklist's short tests pass.

Each deployment lives in `/opt/shakepie/releases/<id>`. To roll back manually:

```text
sudo systemctl stop seisberry-recorder.service
sudo ln -sfn /opt/shakepie/releases/<previous-id> /opt/shakepie/current.next
sudo mv -Tf /opt/shakepie/current.next /opt/shakepie/current
sudo systemctl start seisberry-recorder.service
/opt/shakepie/current/bin/shakepie-health
```

Rollback changes software only. Do not delete, rename, or combine recording files. A `.partial` from the failed release is recovered by a compatible version-1 recorder or preserved for `shakepie-inspect` diagnosis.
