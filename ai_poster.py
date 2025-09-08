import os, json, base64, time, datetime as dt, requests, re, unicodedata, random, pathlib
from typing import Optional, Tuple, List, Set
from dotenv import load_dotenv

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")

load_dotenv()

# --------- ENV ---------
WP_BASE_URL = os.getenv("WP_BASE_URL", "").rstrip("/")
WP_USERNAME = os.getenv("WP_USERNAME", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")
WP_DEFAULT_AUTHOR_ID = int(os.getenv("WP_DEFAULT_AUTHOR_ID", "0"))
WP_DEFAULT_CATEGORY_ID = int(os.getenv("WP_DEFAULT_CATEGORY_ID", "0"))

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "OPENROUTER").upper()
TARGET_LANGUAGE = os.getenv("TARGET_LANGUAGE", "English")
MIN_WORDS = int(os.getenv("MIN_WORDS", "900"))
MAX_WORDS = int(os.getenv("MAX_WORDS", "1300"))
FALLBACK_TOPICS = [x.strip() for x in os.getenv("FALLBACK_TOPICS", "").split(",") if x.strip()]

# Pool / throttling controls
TARGET_DRAFT_POOL = int(os.getenv("TARGET_DRAFT_POOL", "30"))          # total drafts you want to keep
CREATE_LIMIT_PER_RUN = int(os.getenv("CREATE_LIMIT_PER_RUN", "5"))     # 0 = unlimited
DAILY_LOCK = os.getenv("DAILY_LOCK", "true").lower() == "true"

# OpenAI (optional)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# OpenRouter (default)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")

# Custom (optional)
CUSTOM_LLM_URL = os.getenv("CUSTOM_LLM_URL", "")
CUSTOM_LLM_AUTH = os.getenv("CUSTOM_LLM_AUTH", "")

# --------- Local cache / locks ---------
DATA_DIR = pathlib.Path(".cache_aa")
DATA_DIR.mkdir(exist_ok=True)
USED_TITLES_PATH = DATA_DIR / "used_titles.json"
LOCK_PATH = DATA_DIR / f"run-{dt.datetime.now(dt.UTC).strftime('%Y%m%d')}.lock"
# --------- HTTP with retry ---------
def post_with_retry(url, *, headers=None, json_body=None, data_body=None, timeout=60, tries=3, backoff=5):
    last_resp = None
    for i in range(tries):
        resp = requests.post(url, headers=headers, json=json_body, data=data_body, timeout=timeout)
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = backoff * (i + 1)
            print(f"{resp.status_code} from {url}. Retrying in {wait}s...")
            time.sleep(wait)
            last_resp = resp
            continue
        resp.raise_for_status()
        return resp
    if last_resp is not None:
        last_resp.raise_for_status()
    raise RuntimeError("HTTP request failed and no response available.")

# --------- WordPress helpers ---------
def wp_auth_header() -> dict:
    if not (WP_USERNAME and WP_APP_PASSWORD):
        raise RuntimeError("Missing WP_USERNAME or WP_APP_PASSWORD")
    token = base64.b64encode(f"{WP_USERNAME}:{WP_APP_PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}"}

def wp_api(path: str) -> str:
    return f"{WP_BASE_URL}/wp-json/wp/v2{path}"

def wp_health_check():
    r = requests.get(f"{WP_BASE_URL}/wp-json/wp/v2/users/me", headers=wp_auth_header(), timeout=30)
    r.raise_for_status()
    me = r.json()
    print("WP auth OK:", me.get("name"), "(ID:", me.get("id"), ")")

def get_posts_titles(status: str, limit=500) -> List[str]:
    """
    Fetch titles by status ('draft' or 'publish' or 'any')
    """
    headers = wp_auth_header()
    titles = []
    page = 1
    while len(titles) < limit:
        params = {"per_page": 100, "page": page, "status": status, "_fields": "title"}
        r = requests.get(wp_api("/posts"), headers=headers, params=params, timeout=30)
        if r.status_code == 400:
            break
        r.raise_for_status()
        items = r.json()
        if not items:
            break
        for it in items:
            t = (it.get("title") or {}).get("rendered") or ""
            if t:
                titles.append(strip_html(t).strip())
        page += 1
    return titles

# --------- LLM calls ---------
def call_llm(system_prompt: str, user_prompt: str, temperature: float = 0.9) -> str:
    if LLM_PROVIDER == "OPENAI":
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY not set")
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt.strip()},
                {"role": "user", "content": user_prompt.strip()},
            ],
            "temperature": temperature,
        }
        r = post_with_retry(url, headers=headers, json_body=payload, timeout=60)
        return r.json()["choices"][0]["message"]["content"].strip()

    if LLM_PROVIDER == "OPENROUTER":
        key = (OPENROUTER_API_KEY or "").strip()
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY not set")
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": WP_BASE_URL or "https://attractivealbania.com",
            "X-Title": "AttractiveAlbania Draft Builder",
        }
        payload = {
            "model": (OPENROUTER_MODEL or "openai/gpt-4o-mini").strip(),
            "messages": [
                {"role": "system", "content": system_prompt.strip()},
                {"role": "user", "content": user_prompt.strip()},
            ],
            "temperature": temperature,
        }
        r = post_with_retry(url, headers=headers, json_body=payload, timeout=60)
        return r.json()["choices"][0]["message"]["content"].strip()

    if LLM_PROVIDER == "CUSTOM":
        if not CUSTOM_LLM_URL:
            raise RuntimeError("CUSTOM_LLM_URL not set")
        headers = {"Content-Type": "application/json"}
        if CUSTOM_LLM_AUTH:
            headers["Authorization"] = CUSTOM_LLM_AUTH
        payload = {"system": system_prompt.strip(), "prompt": user_prompt.strip()}
        r = post_with_retry(CUSTOM_LLM_URL, headers=headers, json_body=payload, timeout=60)
        return r.text.strip()

    raise RuntimeError(f"Unsupported LLM_PROVIDER: {LLM_PROVIDER}")

def call_llm_ideation(n: int, banned_titles: List[str]) -> List[dict]:
    date_hint = dt.datetime.utcnow().strftime("%Y-%m-%d")
    banned = "; ".join(sorted(set(banned_titles))[:120])

    system_prompt = f"You are an SEO travel editor for a blog about Albania. Reply in {TARGET_LANGUAGE}."
    user_prompt = f"""
Return ONLY valid JSON array (no prose). Each item:
  - title (<= 65 chars; include 'Albania' or a specific city/region)
  - description (<= 140 chars, no quotes)
  - keywords (5-8 items, comma-separated)
Rules:
- Propose exactly {n} unique, high-intent topics for Albania travel.
- Avoid any similarity to these existing or banned titles: {banned}
- No duplicates, no near-duplicates, avoid vague 'Things to do' unless city-specific and unique.
- Prefer specificity (e.g., '2 days in Shkodër' vs 'Albania itinerary').
- Today is {date_hint}. Include seasonal relevance when helpful.
"""
    raw = call_llm(system_prompt, user_prompt, temperature=0.95)
    arr = extract_json_array(raw)
    return [coerce_idea(x) for x in arr]

# --------- JSON helpers ---------
def extract_json_array(text: str) -> List[dict]:
    try:
        j = json.loads(text)
        if isinstance(j, list):
            return j
        if isinstance(j, dict) and "ideas" in j and isinstance(j["ideas"], list):
            return j["ideas"]
    except Exception:
        pass
    m = re.search(r"\[\s*\{.*?\}\s*\]", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return []
    return []

def coerce_idea(x: dict) -> dict:
    t = (x.get("title") if isinstance(x, dict) else "") or ""
    d = (x.get("description") if isinstance(x, dict) else "") or ""
    k = (x.get("keywords") if isinstance(x, dict) else "") or ""
    if isinstance(k, list):
        k = ", ".join(str(s) for s in k)
    return {"title": str(t).strip(), "description": str(d).strip(), "keywords": str(k).strip()}

# --------- De-dup helpers ---------
def normalize_title(t: str) -> str:
    t = strip_html(t).lower()
    t = unicodedata.normalize("NFKD", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")

def unique_new_ideas(ideas: List[dict], banned_norm: Set[str]) -> List[dict]:
    out = []
    seen = set()
    for it in ideas:
        nt = normalize_title(it.get("title", ""))
        if not nt or nt in banned_norm or nt in seen:
            continue
        # filter super-generic
        if nt in {"things to do in albania", "albania travel guide", "albania itinerary"}:
            continue
        seen.add(nt)
        out.append(it)
    return out

def load_used_titles() -> Set[str]:
    if USED_TITLES_PATH.exists():
        try:
            data = json.loads(USED_TITLES_PATH.read_text(encoding="utf-8"))
            return {normalize_title(t) for t in data if isinstance(t, str)}
        except Exception:
            return set()
    return set()

def save_used_titles(titles: Set[str]):
    USED_TITLES_PATH.write_text(json.dumps(sorted(list(titles)), ensure_ascii=False, indent=2), encoding="utf-8")

# --------- Draft creation ---------
def call_llm_article(title: str, description: str, keywords_csv: str) -> str:
    system_prompt = (
        f"You are a senior travel copywriter. Write in {TARGET_LANGUAGE}. "
        "Tone: helpful, accurate, no fluff."
    )
    user_prompt = f"""
Write a {MIN_WORDS}-{MAX_WORDS} word blog article in clean HTML (no <html>, no <body>):
- H1 is the exact title: {title}
- 80-120 word intro
- 5-8 sections with H2/H3: practical tips, general price ranges, best times, transport, safety
- Add 2 internal link placeholders to AttractiveAlbania.com: <a href="/{{path}}">Anchor</a>
- Add a "Plan Your Trip" checklist (<ul>)
- Add 4-question FAQ with <details><summary>Q</summary><p>A</p></details>
- End with a short takeaway
Allowed tags: <p>, <h2>, <h3>, <ul>, <li>, <details>, <summary>, <a>
Target keywords: {keywords_csv}
Context meta description: {description}
"""
    return call_llm(system_prompt, user_prompt, temperature=0.8)

def create_post(title: str, html_content: str, meta_desc: str, keywords: list,
                featured_media: Optional[int] = None) -> Tuple[int, str]:
    headers = wp_auth_header()
    headers["Content-Type"] = "application/json"

    data = {
        "title": title,
        "content": html_content,
        "status": "draft",  # always draft
        "excerpt": meta_desc[:140],
    }
    if WP_DEFAULT_AUTHOR_ID:
        data["author"] = WP_DEFAULT_AUTHOR_ID
    if WP_DEFAULT_CATEGORY_ID:
        data["categories"] = [WP_DEFAULT_CATEGORY_ID]
    if featured_media:
        data["featured_media"] = featured_media

    data["meta"] = {
        "_yoast_wpseo_metadesc": meta_desc[:155],
        "_yoast_wpseo_focuskw": (keywords[0] if keywords else ""),
    }

    r = post_with_retry(wp_api("/posts"), headers=headers, data_body=json.dumps(data), timeout=60)
    j = r.json()
    return j["id"], j.get("link", "")

# --------- Main (idempotent) ---------
def main():
    # daily lock
    if DAILY_LOCK and LOCK_PATH.exists():
        print("Daily lock present; already ran today. Exiting.")
        return

    wp_health_check()

    # Count current drafts & published titles for de-dup
    draft_titles = get_posts_titles(status="draft", limit=500)
    published_titles = get_posts_titles(status="publish", limit=1000)
    current_drafts = len(draft_titles)
    print(f"Existing drafts on WP: {current_drafts}")

    # If we already meet pool target, exit
    needed = max(0, TARGET_DRAFT_POOL - current_drafts)
    if needed == 0:
        print(f"Draft pool already full (TARGET_DRAFT_POOL={TARGET_DRAFT_POOL}). Nothing to do.")
        if DAILY_LOCK:
            LOCK_PATH.write_text("ok", encoding="utf-8")
        return

    # Respect per-run create cap
    if CREATE_LIMIT_PER_RUN > 0:
        needed = min(needed, CREATE_LIMIT_PER_RUN)
    print(f"Will create up to {needed} new drafts this run.")

    # Build banned set = all WP titles + previously used local titles
    used_norm = load_used_titles()
    banned_norm = {normalize_title(t) for t in draft_titles + published_titles} | used_norm

    # Generate ideas until we have 'needed' unique
    collected: List[dict] = []
    attempts = 0
    while len(collected) < needed and attempts < 5:
        attempts += 1
        ask_for = max(needed + 5, 20)
        raw_ideas = call_llm_ideation(ask_for, banned_titles=list(banned_norm)[:150])
        batch = unique_new_ideas(raw_ideas, banned_norm=banned_norm | {normalize_title(i.get('title','')) for i in collected})
        collected.extend(batch)
        print(f"Ideation pass {attempts}: +{len(batch)} new, total={len(collected)}")

        for it in batch:
            banned_norm.add(normalize_title(it.get("title","")))
        time.sleep(random.uniform(0.3, 0.8))

    if len(collected) < needed and FALLBACK_TOPICS:
        for t in FALLBACK_TOPICS:
            nt = normalize_title(t)
            if nt not in banned_norm and len(collected) < needed:
                collected.append({"title": t, "description": "Travel guide", "keywords": "Albania travel"})

    if len(collected) == 0:
        print("No new unique ideas found. Exiting.")
        return

    # Create the drafts
    new_used = set(used_norm)
    created = 0
    for idea in collected[:needed]:
        title = (idea.get("title") or "Albania Travel Guide").strip()
        meta = (idea.get("description") or "Travel tips for Albania").strip()
        keywords_list = [k.strip() for k in (idea.get("keywords") or "").split(",") if k.strip()]

        html = call_llm_article(title, meta, ", ".join(keywords_list))
        post_id, post_link = create_post(
            title=title,
            html_content=html,
            meta_desc=meta,
            keywords=keywords_list,
            featured_media=None,
        )
        print(f"Draft created: ID {post_id} → {post_link}")
        new_used.add(normalize_title(title))
        created += 1

    save_used_titles(new_used)
    print(f"Done. Created {created} drafts.")
    if DAILY_LOCK:
        LOCK_PATH.write_text("ok", encoding="utf-8")

# ---- utils
def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")

if __name__ == "__main__":
    main()
