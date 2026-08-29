# a2a-hub systemd units

Two units run the site, plus a timer that backs up the database.

## a2a-hub.service

The app itself: pure-stdlib Python listening on 127.0.0.1:8787.

```bash
sudo cp a2a-hub.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now a2a-hub
```

## cloudflared.service

The public entrance: a Cloudflare Tunnel (outbound-only connection, origin IP
hidden, no open ports). DNS CNAMEs for qianyu0204.site / www / api point at
the tunnel; ingress rules live in `~/.cloudflared/config.yml`, mapping the
hostnames to `http://localhost:8787`.

## a2a-backup.timer

Online sqlite backup every 6 hours (`backup.sh`), 14-day retention.
