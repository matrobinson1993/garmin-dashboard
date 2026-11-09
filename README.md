# Garmin Dashboard

Your own private dashboard for your Garmin Connect data. It opens on any phone or computer, and you can add it to your home screen like an app.

- **Your own copy.** Your Garmin login is stored as a secret in **your** GitHub account. Nobody else ever sees it, including whoever made this template.
- **Encrypted.** Your data is encrypted with a passphrase you choose. The web page is public, but without the passphrase it's unreadable. Your phone decrypts it locally.
- **Automatic.** GitHub downloads new data from Garmin once a day.
- **Free.** It uses GitHub's free hosting and automation for public repositories.

What you get: activities (distance, pace, heart rate, elevation, training effect), steps, resting heart rate, HRV, stress, Body Battery, sleep stages and scores, training readiness, VO₂ max and weight, with charts for the last 30 days, 90 days, 1 year or everything.

---

## Setup (about 10 minutes, works on a phone)

You need a free GitHub account: https://github.com/signup

### 1. Make your copy
At the top of this page, tap **Use this template → Create a new repository**.
- Name it anything, e.g. `my-garmin`.
- Leave it **Public**. Free GitHub Pages needs a public repository, and your data is encrypted.
- Tap **Create repository**.

> On a phone, if you can't see the button, open your browser menu and choose **Desktop site**.

### 2. Turn on the web page
In **your new repository**: **Settings → Pages**. Under **Build and deployment → Source**, choose **GitHub Actions**.

### 3. Add your secrets
**Settings → Secrets and variables → Actions → New repository secret.** Add these three:

| Name | Value |
|---|---|
| `GARMIN_EMAIL` | your Garmin Connect email |
| `GARMIN_PASSWORD` | your Garmin Connect password |
| `DATA_PASSPHRASE` | a new passphrase you'll type to open the dashboard. Use something long, like 4–5 random words. **Don't reuse your Garmin password.** |

Write the passphrase down. If you lose it, you can still start over (see below), but you can't recover the old encrypted data without it.

### 4. Run the first sync
Go to the **Actions** tab. If asked, tap **I understand my workflows, go ahead and enable them**. Then tap **Sync Garmin data → Run workflow → Run workflow**.

**If you use 2-step verification on Garmin:** the first run finishes with a yellow warning saying a code is needed, and Garmin emails or texts you a code. Tap **Run workflow** again, paste the code into **Garmin verification code**, and run it. Do this within a few minutes, before the code expires. You only need to do this once. After that, a saved login is used.

### 5. Open your dashboard
Your dashboard is at:

```
https://YOUR-GITHUB-USERNAME.github.io/YOUR-REPO-NAME/
```

The link also appears on the finished run in the Actions tab. Enter your passphrase, and tick **Remember on this device** if you like.

To add it to your home screen: in Chrome on Android, tap **⋮ → Add to Home screen**. In Safari on iPhone, tap **Share → Add to Home Screen**.

**First-time download:** each run downloads for up to about an hour, publishes what it has, and then starts the next run automatically, until all your history is in. Several years of history can take a few hours in total. The page works from the end of the first run.

---

## Everyday use
- It syncs automatically every day. To sync right away, go to **Actions → Sync Garmin data → Run workflow**. The dashboard's **More** tab links straight to it.
- **Start from a specific date:** when you run the workflow, fill in the **first date** box (e.g. `2020-01-01`).
- **Changed your passphrase?** Update the `DATA_PASSPHRASE` secret, then run the workflow with **Start over** ticked.
- **Changed your Garmin password?** Update `GARMIN_PASSWORD` and run the workflow.

## Troubleshooting
| Problem | Fix |
|---|---|
| Run failed: "Set a DATA_PASSPHRASE…" | Add the secret (step 3). Secret names must match exactly. |
| Run failed: "could not be decrypted…" | `DATA_PASSPHRASE` was changed. Run once with **Start over** ticked. |
| "A verification code was entered, but there is no login waiting" | The code was used or expired. Run without a code first, then again with the new code. |
| Page says "No data yet" | The first run hasn't finished. Check the Actions tab. |
| Page shows 404 | Step 2: Pages source must be **GitHub Actions**. |
| Login errors after it worked before | Garmin sometimes changes its login. In your repo, tap **Sync fork** (or re-create from the template) to get the latest version, then run again. |
| Daily sync stopped | GitHub pauses scheduled runs in repositories with no activity for 60 days. Open Actions and re-enable the workflow, or run it manually. |

## Privacy & security notes
- Your Garmin email and password are GitHub **secrets**. They're never shown in logs and are only available to your own workflow.
- Everything kept between runs is encrypted with your passphrase using AES-256-GCM, with a key derived through PBKDF2-SHA256 at 600,000 rounds. That includes your data, the saved Garmin login and any half-finished 2-step login.
- The published page and `data.enc.json` are public files, so your passphrase is what protects them. Use a long one.
- Run logs in a public repository are visible to anyone. The script prints only counts, never names, places or numbers about you. A 2-step code you type into the run form does appear in that run's details, but it works only once and expires within minutes.
- This uses Garmin's **unofficial** API (the [`garminconnect`](https://pypi.org/project/garminconnect/) Python library), the same way the Garmin Connect app talks to Garmin. It isn't endorsed by Garmin, and it can break when Garmin changes things. Use it at your own risk.

## How it works
```
GitHub Actions (daily) ──► garmin_sync.py ──► Garmin Connect
        │                        │
        │                        └─► encrypts data with DATA_PASSPHRASE
        ▼
GitHub Pages: site/index.html + data.enc.json ──► your phone decrypts it in the browser
```
- `garmin_sync.py`: logs in, fetches only what's new, handles 2-step verification and rate limits, and writes the encrypted data.
- `site/index.html`: the dashboard, a single file with no external dependencies.
- `.github/workflows/sync.yml`: the daily schedule, the manual "Run workflow" form and publishing.
