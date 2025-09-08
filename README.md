# AttractiveAlbania Daily Auto-Poster (Python)

This script creates **one scheduled WordPress post per day** using an LLM (GPT) to draft content, then posts via the **WordPress REST API** with **Application Passwords**.

## 1) WordPress prep
- Create an **Application Password**: WP Admin > Users > Your Profile > Application Passwords.
- Ensure `/wp-json/wp/v2/posts` is reachable (security plugins/firewalls must allow it).

## 2) Setup
```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your secrets and preferences
```

## 3) Test (creates tomorrow's scheduled post)
```bash
python ai_poster.py
```

## 4) Cron (Europe/Tirane)
Run once per day at **09:45** local (approx 07:45 UTC in summer):
```bash
crontab -e
```
Add a line (adjust paths):
```
45 9 * * * cd /path/to/aa_auto_poster_python && /path/to/aa_auto_poster_python/.venv/bin/python ai_poster.py >> poster.log 2>&1
```

## Tuning
- To publish immediately, change `schedule_utc` to `None` and set `"status": "publish"` in `create_post()`.
- Add featured images by uploading to `/wp-json/wp/v2/media` and passing `featured_media` id.
- Avoid duplicate topics by searching existing titles with `GET /wp-json/wp/v2/posts?search=...` before creating a new one.
- Rotate languages by changing `TARGET_LANGUAGE` or by running with different env files per day.
