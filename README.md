# Bottle Tracker

Watches online liquor stores for hard-to-find whiskey bottles and alerts me the moment one comes into stock. A public page shows what is on the shelf right now.

**Live page:** https://larboz.github.io/Bottle-Tracker/

## What it does

- Checks store websites several times a day for a list of target bottles
- Emails me when a bottle changes from not listed to in stock (no repeat alerts)
- Publishes a web page of current in-stock bottles with photos, prices and store links
- Tracks when a store's New Arrivals page changes, to learn when inventory is loaded

## How it works

- **Python + Playwright** load each store's search page in a headless browser, since many stores render products with JavaScript
- **Fuzzy but strict matching:** a bottle must match on words that sit close together in the product name, with an exclusion list to filter out look-alikes (cigars, rum finishes, glassware)
- **Multiple spellings** per bottle are supported (for example `CYPB | Craft Your Perfect Bourbon`)
- **Shopify stores** are read directly from their product data, no browser needed
- **GitHub Actions** runs everything on a schedule, keeps state in the repo, and sends email through SMTP
- **GitHub Pages** hosts the static page, which reads `data.json`

## Files

| File | Purpose |
| --- | --- |
| `config.yaml` | Stores and bottles to watch |
| `checker.py` | Stock checker, email alerts, writes `data.json` |
| `arrivals.py` | New Arrivals change tracker |
| `index.html` | The public page |
| `.github/workflows/` | Schedules for the checker and tracker |

## Setup

1. Fork or copy this repo
2. Edit `config.yaml`
3. Add repo secrets: `SMTP_USER`, `SMTP_PASS` (app password), `ALERT_TO`
4. Turn on GitHub Pages (Settings, Pages, deploy from `main`, root folder)
5. Run the Bottle check workflow once from the Actions tab
