# NavidromeBot-ML

Telegram Navidrome account bot with multi-server synchronization and playlist import.

## Features

- Create, delete and reset Navidrome users
- Bind / unbind an existing account to a Telegram user (`/bind`, `/unbind`, `/mybind`)
- Synchronize users across multiple Navidrome servers
- Pending synchronization queue and `/sync`
- `/health` server health check
- `/gd <playlist URL>` playlist import entry point
- NetEase, QQ Music, Kuwo and Kugou playlist adapters
- MongoDB backup and restore

## Security

Never commit Telegram tokens, Navidrome passwords, MongoDB passwords, platform cookies, real server configuration, logs or database files.

Copy `config.example.json` to `config.json` and `.env.example` to `.env`; both real files are ignored by `.gitignore`.

## Docker

```bash
cp config.example.json config.json
cp .env.example .env
mkdir -p data/db data/db_backup data/logs
docker compose up -d --build
```

## Multi-server configuration

Configure the `NAVIDROME_SERVERS` array in `config.json`. The first server is the primary server. Additional servers are synchronized by username. After adding a server, an administrator can run `/sync`.

## Account binding

If a user already has an account on the Navidrome servers but the bot database has
no record for it (for example after an incomplete restore), the user can link the
account themselves:

```text
/bind              enter account name, then the account password
/bind <username>   same, in one step
/unbind            remove the Telegram link (the Navidrome account is kept)
/mybind            show the current link
```

The password is verified against Navidrome's `/auth/login` and is **never stored**.
An account already linked to another Telegram user is rejected. Unbinding only
removes the database record and leaves the music account untouched, so it can be
bound again later.

## Playlist import

Users can send `/gd <playlist URL>` in a private chat. The bot reports matched and missing songs. External playlist web interfaces may change and require maintenance.

## Version

0.2.0-ml (2026-11-12)
