import json
import os
import re
import hashlib
import time
from datetime import datetime
from flask import Flask, render_template, request, jsonify
import feedparser
import requests
from urllib.parse import urljoin, urlparse, urlencode

app = Flask(__name__)
FEEDS_FILE = 'feeds.json'
COOKIE_CACHE = {}

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
        return False

    try:
        preact_data = json.loads(preact_match.group(1))
    except json.JSONDecodeError:
        return False

    challenge = preact_data.get('challenge', '')
    difficulty = preact_data.get('difficulty', 4)
    redir = preact_data.get('redir', '/')

    result = hashlib.sha256(challenge.encode('utf-8')).hexdigest()

    wait_time = difficulty * 0.125 + 0.5
    time.sleep(wait_time)

    if '?' in redir:
        pass_url = f"{redir}&result={result}"
    else:
        pass_url = f"{redir}?result={result}"

    if not pass_url.startswith('http'):
        pass_url = urljoin(base_url, pass_url)

    try:
        resp = session.get(pass_url, headers=REQUEST_HEADERS, timeout=30, allow_redirects=True)
        return True
    except Exception:
        return False


def fetch_with_anubis(url):
    domain = get_domain(url)
    session = requests.Session()

    if domain in COOKIE_CACHE and COOKIE_CACHE[domain].get('expires', 0) > time.time():
        for name, value in COOKIE_CACHE[domain]['cookies'].items():
            session.cookies.set(name, value, domain=urlparse(domain).netloc)

    resp = session.get(url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True)

    is_challenge = '<html' in resp.text[:500].lower() and 'anubis' in resp.text.lower()
    if not is_challenge:
        is_challenge = '<html' in resp.text[:500].lower() and 'not a bot' in resp.text.lower()

    if is_challenge:
        solved = solve_anubis(resp.text, domain, session)
        if solved:
            COOKIE_CACHE[domain] = {
                'cookies': {c.name: c.value for c in session.cookies},
                'expires': time.time() + 3600
            }
            resp = session.get(url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True)

    return resp, session


def fetch_feed_items(url):
    try:
        resp, session = fetch_with_anubis(url)

        if '<html' in resp.text[:500].lower():
            return []

        feed = feedparser.parse(resp.text)
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
            })
        return items
    except Exception:
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


@app.route('/api/test-feed')
def test_feed():
    url = request.args.get('url', 'https://nitter.privacyredirect.com/BeckyLynchWWE/rss')
    try:
        resp, session = fetch_with_anubis(url)
        is_html = '<html' in resp.text[:500].lower()
        feed = feedparser.parse(resp.text)
        return jsonify({
            'url': url,
            'status_code': resp.status_code,
            'content_type': resp.headers.get('Content-Type', ''),
            'is_html': is_html,
            'entries_count': len(feed.entries),
            'feed_title': feed.feed.get('title', 'N/A'),
            'cookies': {c.name: c.value for c in session.cookies},
            'raw_snippet': resp.text[:500],
        })
    except Exception as e:
        return jsonify({'url': url, 'error': str(e)})


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)