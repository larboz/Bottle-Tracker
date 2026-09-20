# Bottle Watch

1. Create a GitHub repo, upload these files (keep the `.github` folder).
2. Edit `config.yaml` with your stores and bottles.
3. Gmail: turn on 2-step verification, create an App Password.
4. Repo > Settings > Secrets and variables > Actions, add:
   - `SMTP_USER` (your Gmail address)
   - `SMTP_PASS` (the app password)
   - `ALERT_TO` (where alerts go)
5. Actions tab > "Bottle check" > Run workflow to test.

You're alerted only when a bottle flips from out of stock to in stock.
Edit the cron line in `.github/workflows/check.yml` to change times.
