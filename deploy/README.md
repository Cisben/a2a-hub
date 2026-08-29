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

The public entrance. Inbound ports are blocked on this host, so the site is
served through a Cloudflare Tunnel (outbound-only connection, origin IP
hidden). See the README: DNS CNAMEs for qianyu0204.site / www / api point at
tunnel id a67ddb0d-1259-4c4e-8da2-3a2d212ee3cd; ingress rules live in
`~/.cloudflared/config.yml`.

## a2a-backup.timer

Online sqlite backup every 6 hours (`backup.sh`), 14-day retention.
