import json
import os
import re
import hashlib
import time
from datetime import datetime
from flask import Flask, render_template, request, jsonify
import feedparser
import requests

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


def solve_anubis_challenge(html_content, base_url, session):
    challenge_match = re.search(r'data-challenge="([^"]+)"', html_content)
    if not challenge_match:
        challenge_match = re.search(r'"challenge"\s*:\s*"([^"]+)"', html_content)
    if not challenge_match:
        return None

    challenge = challenge_match.group(1)

    difficulty_match = re.search(r'data-difficulty="(\d+)"', html_content)
    if not difficulty_match:
        difficulty_match = re.search(r'"difficulty"\s*:\s*(\d+)', html_content)
    difficulty = int(difficulty_match.group(1)) if difficulty_match else 5

    nonce = 0
    target_prefix = '0' * difficulty
    while True:
        hash_input = f"{challenge}{nonce}".encode('utf-8')
        hash_result = hashlib.sha256(hash_input).hexdigest()
        if hash_result.startswith(target_prefix):
            break
        nonce += 1
        if nonce > 5000000:
            return None

    try:
        api_url = base_url.rstrip('/') + '/.within.website/x/anubis/api/pass-challenge'
        resp = session.post(api_url, data={
            'challenge': challenge,
            'nonce': nonce,
            'redir': '/rss' if '/rss' in base_url else base_url.split('.com')[1] if '.com' in base_url else '/'
        }, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True)
        return dict(session.cookies)
    except Exception:
        return None


def get_domain(url):
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def fetch_with_anubis_bypass(url, session=None):
    if session is None:
        session = requests.Session()

    domain = get_domain(url)

    if domain in COOKIE_CACHE and COOKIE_CACHE[domain].get('expires', 0) > time.time():
        session.cookies.update(COOKIE_CACHE[domain]['cookies'])

    resp = session.get(url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True)

    if '<html' in resp.text[:500].lower() and ('not a bot' in resp.text.lower() or 'anubis' in resp.text.lower()):
        cookies = solve_anubis_challenge(resp.text, domain, session)
        if cookies:
            COOKIE_CACHE[domain] = {
                'cookies': cookies,
                'expires': time.time() + 3600
            }
            resp = session.get(url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True)

    return resp, session


def fetch_feed_items(url):
    try:
        resp, session = fetch_with_anubis_bypass(url)

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
    except Exception as e:
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
        resp, session = fetch_with_anubis_bypass(url)
        is_html = '<html' in resp.text[:500].lower()
        feed = feedparser.parse(resp.text)
        return jsonify({
            'url': url,
            'status_code': resp.status_code,
            'content_type': resp.headers.get('Content-Type', ''),
            'is_html': is_html,
            'entries_count': len(feed.entries),
            'feed_title': feed.feed.get('title', 'N/A'),
            'bozo': feed.bozo,
            'raw_snippet': resp.text[:500],
        })
    except Exception as e:
        return jsonify({'url': url, 'error': str(e)})


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)