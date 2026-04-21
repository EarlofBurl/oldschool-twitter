import json
import os
from datetime import datetime
from flask import Flask, render_template, request, jsonify
import feedparser
import requests

app = Flask(__name__)
FEEDS_FILE = 'feeds.json'

REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/rss+xml, application/xml, text/xml, application/atom+xml, */*',
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


def fetch_feed_items(url):
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', '')
        raw_content = resp.text

        if '<html' in raw_content[:500].lower():
            return []

        feed = feedparser.parse(raw_content)
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


def extract_nitter_urls(html_text):
    import re
    pattern = r'https?://[^\s"\'<>]+'
    urls = re.findall(pattern, html_text)
    return [u for u in urls if 'nitter' in u.lower() or 'twitter' in u.lower()]


def fetch_nitter_html(username, base_url):
    try:
        profile_url = base_url.rstrip('/') + '/' + username
        resp = requests.get(profile_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml',
            'Accept-Language': 'en-US,en;q=0.9',
        }, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except Exception:
        return None


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
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True)
        content_type = resp.headers.get('Content-Type', '')
        raw = resp.text[:1000]
        is_html = '<html' in raw.lower()
        feed = feedparser.parse(resp.text)
        return jsonify({
            'url': url,
            'status_code': resp.status_code,
            'content_type': content_type,
            'is_html': is_html,
            'entries_count': len(feed.entries),
            'feed_title': feed.feed.get('title', 'N/A'),
            'bozo': feed.bozo,
            'bozo_exception': str(feed.bozo_exception) if feed.bozo else None,
            'raw_snippet': raw[:500],
        })
    except Exception as e:
        return jsonify({'url': url, 'error': str(e)})


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)