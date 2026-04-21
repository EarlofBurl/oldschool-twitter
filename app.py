import json
import os
import re
import hashlib
import time
import logging
from datetime import datetime
from flask import Flask, render_template, request, jsonify
import feedparser
import requests
from urllib.parse import urljoin, urlparse

app = Flask(__name__)
FEEDS_FILE = 'feeds.json'
COOKIE_CACHE = {}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Cache-Control': 'no-cache',
}


def load_feeds():
    if os.path.exists(FEEDS_FILE):
        with open(FEEDS_FILE, 'r') as f:
            return json.load(f)
    return []


def save_feeds(feeds):
    with open(FEEDS_FILE, 'w') as f:
        json.dump(feeds, f, indent=2)


def parse_date(date_str):
    for fmt in ['%a, %d %b %Y %H:%M:%S %Z', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d %H:%M:%S']:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            pass
    return datetime.now()


def get_domain(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def solve_anubis(html_content, base_url, session):
    preact_match = re.search(
        r'<script\s+id="preact_info"\s+type="application/json">(.*?)</script>',
        html_content, re.DOTALL
    )
    if not preact_match:
        logger.warning("No preact_info found in challenge page")
        return None

    try:
        preact_data = json.loads(preact_match.group(1))
    except json.JSONDecodeError:
        logger.warning("Failed to parse preact_info JSON")
        return None

    challenge = preact_data.get('challenge', '')
    difficulty = preact_data.get('difficulty', 4)
    redir = preact_data.get('redir', '/')

    logger.info(f"Solving Anubis challenge (difficulty={difficulty}) for {base_url}")

    result = hashlib.sha256(challenge.encode('utf-8')).hexdigest()

    wait_time = difficulty * 0.125 + 0.5
    logger.info(f"Waiting {wait_time:.1f}s (difficulty={difficulty})")
    time.sleep(wait_time)

    if '?' in redir:
        pass_url = f"{redir}&result={result}"
    else:
        pass_url = f"{redir}?result={result}"

    if not pass_url.startswith('http'):
        pass_url = urljoin(base_url, pass_url)

    logger.info(f"Passing challenge: {pass_url}")

    try:
        resp = session.get(pass_url, headers=REQUEST_HEADERS, timeout=30, allow_redirects=True)
        logger.info(f"Challenge response: status={resp.status_code}, content-type={resp.headers.get('Content-Type', '')}")

        if '<html' in resp.text[:500].lower() and 'anubis' in resp.text.lower():
            logger.warning("Challenge not solved - still getting challenge page after attempt")
            return None

        logger.info("Anubis challenge solved successfully!")
        return resp
    except Exception as e:
        logger.error(f"Failed to solve Anubis challenge: {e}")
        return None


def fetch_with_anubis(url):
    domain = get_domain(url)
    session = requests.Session()

    cached = COOKIE_CACHE.get(domain)
    if cached and cached.get('expires', 0) > time.time():
        logger.info(f"Using cached cookies for {domain}")
        for name, value in cached['cookies'].items():
            session.cookies.set(name, value, domain=urlparse(domain).netloc)

    resp = session.get(url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True)

    is_challenge = '<html' in resp.text[:500].lower() and ('anubis' in resp.text.lower() or 'not a bot' in resp.text.lower())

    if is_challenge:
        logger.info(f"Anubis challenge detected for {url}")
        challenge_resp = solve_anubis(resp.text, domain, session)
        if challenge_resp is not None:
            COOKIE_CACHE[domain] = {
                'cookies': {c.name: c.value for c in session.cookies},
                'expires': time.time() + 3600
            }
            if challenge_resp.headers.get('Content-Type', '').startswith('application/rss') or '<rss' in challenge_resp.text[:100]:
                logger.info("Got RSS content directly from challenge redirect")
                return challenge_resp, session
            resp = session.get(url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True)
        else:
            logger.warning(f"Failed to solve Anubis challenge for {url}")

    return resp, session


def fetch_feed_items(url):
    try:
        resp, session = fetch_with_anubis(url)

        if '<html' in resp.text[:500].lower():
            logger.error(f"Got HTML instead of RSS for {url}")
            return []

        feed = feedparser.parse(resp.text)
        if not feed.entries:
            logger.warning(f"No entries in feed for {url}")
            return []

        items = []
        for entry in feed.entries:
            date_str = entry.get('published', '')
            timestamp = parse_date(date_str).timestamp()
            creator = getattr(entry, 'author', getattr(entry, 'dc_creator', ''))
            title = entry.get('title', '')
            description = entry.get('description', entry.get('summary', ''))
            link = entry.get('link', '')
            profile_image = ''
            if feed.feed.get('image'):
                profile_image = feed.feed.image.get('href', '')
            elif hasattr(feed.feed, 'image'):
                profile_image = getattr(feed.feed.image, 'href', '')

            is_retweet = bool(title.startswith('RT by'))
            rt_creator = ''
            if is_retweet:
                rt_match = re.match(r'RT by @(\S+):', title)
                if rt_match:
                    rt_creator = rt_match.group(1)

            items.append({
                'title': title,
                'description': description,
                'link': link,
                'date': date_str,
                'timestamp': timestamp,
                'creator': creator,
                'profile_image': profile_image,
                'feed_title': feed.feed.get('title', url),
                'feed_link': feed.feed.get('link', url),
                'is_retweet': is_retweet,
                'rt_creator': rt_creator,
            })
        logger.info(f"Fetched {len(items)} items from {url}")
        return items
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return []


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/feeds', methods=['GET'])
def get_feeds():
    feeds = load_feeds()
    return jsonify(feeds)


@app.route('/api/feeds', methods=['POST'])
def add_feed():
    data = request.get_json()
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL required'}), 400
    feeds = load_feeds()
    if url not in feeds:
        feeds.append(url)
        save_feeds(feeds)
    return jsonify({'feeds': feeds})


@app.route('/api/feeds', methods=['DELETE'])
def remove_feed():
    data = request.get_json()
    url = data.get('url', '').strip()
    feeds = load_feeds()
    if url in feeds:
        feeds.remove(url)
        save_feeds(feeds)
    return jsonify({'feeds': feeds})


@app.route('/api/timeline', methods=['GET'])
def get_timeline():
    feeds = load_feeds()
    all_items = []
    errors = []
    for feed_url in feeds:
        items = fetch_feed_items(feed_url)
        if not items:
            errors.append({'url': feed_url, 'error': 'No items or fetch failed'})
        all_items.extend(items)
    all_items.sort(key=lambda x: x['timestamp'], reverse=True)
    return jsonify({
        'items': all_items[:50],
        'feeds_count': len(feeds),
        'errors': errors
    })


@app.route('/proxy/pic')
def proxy_pic():
    url = request.args.get('url', '')
    if not url:
        return '', 400
    try:
        resp, session = fetch_with_anubis(url)
        content_type = resp.headers.get('Content-Type', 'image/jpeg')
        return resp.content, 200, {'Content-Type': content_type, 'Cache-Control': 'public, max-age=3600'}
    except Exception:
        return '', 500


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'cookie_cache': {k: {'expires': v['expires']} for k, v in COOKIE_CACHE.items()}})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)