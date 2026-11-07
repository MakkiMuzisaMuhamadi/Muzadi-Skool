# Putting the school system on a VPS (many schools, one server)

Every school is a **subdomain** of your domain, all served by **one codebase and one
PostgreSQL database**:

| Address | Who |
|---|---|
| `admin.muzaditechnologies.com` | you (Muzadi, SUPER_ADMIN): all schools, subscriptions, SMS credit, release notes |
| `stmarys.muzaditechnologies.com` | St. Mary's staff, students, parents |
| `kings.muzaditechnologies.com` | King's College staff, students, parents |

Adding a school needs **no** DNS, nginx or certificate change: you create it at
`/platform/schools/new/` and its address works immediately.

Paths below assume the app lives in `/srv/school360/app` and runs as the Linux user
`school360`. Replace `muzaditechnologies.com` with your domain.

---

## 1. The server

* Ubuntu 24.04 LTS, start with **2 vCPU / 4 GB RAM / 80 GB SSD** (WordPress sites on the
  same VPS need more RAM: plan 6–8 GB). Upgrade the plan before report-card week if needed.
* Set the clock to Kampala time: `sudo timedatectl set-timezone Africa/Kampala`

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3-venv python3-dev build-essential git nginx postgresql redis-server \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libffi-dev shared-mime-info \
    ufw fail2ban unattended-upgrades rclone certbot
sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable
sudo adduser --system --group --home /srv/school360 school360
sudo mkdir -p /srv/school360/{logs,backups} && sudo chown -R school360:www-data /srv/school360
```

Log in with an SSH key and switch off password logins (`PasswordAuthentication no`
in `/etc/ssh/sshd_config`).

## 2. DNS (at your domain registrar or Cloudflare)

| Type | Name | Value |
|---|---|---|
| A | `muzaditechnologies.com` | your VPS IP |
| A | `*` (wildcard) | your VPS IP |

The wildcard record is what makes every new school's address work automatically.

## 3. Database

```bash
sudo -u postgres psql -c "CREATE USER school360 WITH PASSWORD 'a-long-random-password';"
sudo -u postgres psql -c "CREATE DATABASE school360 OWNER school360;"
```

Moving your current data: copy `zimak_backup.dump` to the server, then
`deploy/scripts/restore.sh zimak_backup.dump school360`.

## 4. The app

```bash
sudo -u school360 git clone https://github.com/MakkiMuzisaMuhamadi/Muzadi-Skool.git /srv/school360/app
sudo -u school360 python3 -m venv /srv/school360/venv
sudo -u school360 /srv/school360/venv/bin/pip install -r /srv/school360/app/requirements.txt
sudo -u school360 cp /srv/school360/app/.env.example /srv/school360/app/.env
sudo -u school360 nano /srv/school360/app/.env          # fill in every value
cd /srv/school360/app
sudo -u school360 /srv/school360/venv/bin/python manage.py migrate
sudo -u school360 /srv/school360/venv/bin/python manage.py collectstatic --noinput
sudo -u school360 /srv/school360/venv/bin/python manage.py setup_platform_admin --username muzadi --email you@example.com
sudo -u school360 /srv/school360/venv/bin/python manage.py check --deploy
```

## 5. One certificate for every school (wildcard SSL)

A wildcard certificate (`*.muzaditechnologies.com`) needs a **DNS check**. Easiest with
Cloudflare DNS (free):

```bash
sudo apt -y install python3-certbot-dns-cloudflare
sudo mkdir -p /root/.secrets && sudo nano /root/.secrets/cloudflare.ini
#   dns_cloudflare_api_token = <token with Zone:DNS:Edit for your domain>
sudo chmod 600 /root/.secrets/cloudflare.ini
sudo certbot certonly --dns-cloudflare --dns-cloudflare-credentials /root/.secrets/cloudflare.ini \
     -d muzaditechnologies.com -d '*.muzaditechnologies.com'
```

Certbot renews it automatically. (Other DNS providers have their own certbot plugin; with
no plugin you must add a TXT record by hand every 90 days – avoid that.)

A school that wants its own domain (e.g. `portal.stmarys.ac.ug`): they point it at your IP,
you enter it as "custom domain" on the school's platform page, add it to `ALLOWED_HOSTS`
and `CSRF_TRUSTED_ORIGINS` in `.env`, and get a normal certificate for it.

## 6. nginx and the background services

```bash
cd /srv/school360/app/deploy
sudo cp nginx/school360.conf /etc/nginx/sites-available/school360
sudo ln -s /etc/nginx/sites-available/school360 /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now school360-gunicorn school360-celery school360-celery-reports school360-celerybeat

sudo cp cron/school360 /etc/cron.d/school360
# let deploy.sh restart the services without a password:
echo 'school360 ALL=(root) NOPASSWD: /bin/systemctl restart school360-gunicorn school360-celery school360-celery-reports school360-celerybeat' \
  | sudo tee /etc/sudoers.d/school360
```

WordPress sites: give each its own server block with its own domain
(`nginx/wordpress-example.conf`). They never clash with the wildcard block.

## 7. First schools

1. Sign in at `https://admin.muzaditechnologies.com` with the platform admin.
2. Your existing school: open **All Schools (Platform)** → the school → give it a
   **subdomain** (e.g. `zimak`) and save. It now opens at `zimak.muzaditechnologies.com`.
   Make sure its head is a **School Admin (ADMIN)**, not SUPER_ADMIN – SUPER_ADMIN is only you.
3. New school: **New school** → name, subdomain, levels, plan, trial days, free SMS units,
   first admin username. Send the admin the address, username and starting password shown.
   They get a setup checklist when they sign in.

## 8. Upgrading – all schools at once

All schools run the same code, so **one deploy upgrades every school**:

```bash
git push                                  # from your laptop
ssh you@server
sudo -u school360 /srv/school360/app/deploy/scripts/deploy.sh          # or a tag: deploy.sh v2.4
```

`deploy.sh` backs up, shows the maintenance page, pulls, installs, migrates, collects
static files, checks and restarts – about a minute. Then tell the schools what changed:
**Platform → Release notes → New note** (tick schools to give a feature to a pilot school
first). Everybody sees a red count on **What's New**.

Rules:
* Deploy in the evening or at the weekend, never in report-card week.
* Test on a staging copy first (a second app folder + a copy of the database, served at
  `staging.muzaditechnologies.com` by its own nginx block – a more specific `server_name`
  wins over the wildcard).
* Migrations must be safe for all schools: add columns with defaults, never rename/drop
  in the same release that stops using them.

## 9. Backups (nightly, off the server)

`deploy/scripts/backup.sh` (cron at 02:15) dumps the database and the uploaded files,
keeps 14 days, and copies them **off the server** with rclone:

```bash
sudo -u school360 rclone config           # e.g. a Backblaze B2 or Google Drive remote "b2"
# then in .env:  BACKUP_REMOTE=b2:muzadi-backups
```

Once a month, prove you can restore:
`deploy/scripts/restore.sh /srv/school360/backups/daily/<latest>.dump school360_restore_test`

## 10. Knowing when something breaks

* **Sentry** (free tier): create a Django project, put its DSN in `SENTRY_DSN`. Errors
  arrive by email with the page and the line of code. Personal data is not sent.
* **Uptime check**: UptimeRobot (free) on `https://admin.muzaditechnologies.com/login/`.
* Logs: `/srv/school360/logs/school360.log`, `journalctl -u school360-gunicorn -f`.
* Lines "Cross-school access blocked" in the log mean a page tried to show another
  school's data and was stopped – tell your developer which page.

## 11. When you grow

| Schools | Setup |
|---|---|
| 1–20 | this one VPS |
| 20–60 | PostgreSQL on its own server (`DB_HOST`), more RAM, more report workers |
| 60–100+ | 2+ app servers behind a load balancer, managed PostgreSQL, uploaded files in object storage |

No code changes are needed for these steps.
