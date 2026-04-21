import json
import os
import re
import hashlib
import time
import logging
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_from_directory
import feedparser
import requests
from urllib.parse import urljoin, urlparse

app = Flask(__name__)
DATA_DIR = '/app/data'
FEEDS_FILE = os.path.join(DATA_DIR, 'feeds.json')
TWEETS_FILE = os.path.join(DATA_DIR, 'tweets.json')
IMAGES_DIR = os.path.join(DATA_DIR, 'images')
COOKIE_CACHE = {}

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

if not os.path.exists(FEEDS_FILE):
    with open(FEEDS_FILE, 'w') as f:
        json.dump([], f)

if not os.path.exists(TWEETS_FILE):
    with open(TWEETS_FILE, 'w') as f:
        json.dump({}, f)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Cache-Control': 'no-cache',
}


def load_feeds():
    with open(FEEDS_FILE, 'r') as f:
        return json.load(f)


def save_feeds(feeds):
    with open(FEEDS_FILE, 'w') as f:
        json.dump(feeds, f, indent=2)


def load_tweets():
    with open(TWEETS_FILE, 'r') as f:
        return json.load(f)


def save_tweets(tweets):
    with open(TWEETS_FILE, 'w') as f:
        json.dump(tweets, f, indent=2, ensure_ascii=False)


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


def download_image(url):
    if not url:
        return None
    url_hash = hashlib.md5(url.encode()).hexdigest()
    ext = '.jpg'
    if '.png' in url.lower():
        ext = '.png'
    elif '.gif' in url.lower():
        ext = '.gif'
    elif '.webp' in url.lower():
        ext = '.webp'
    filename = f"{url_hash}{ext}"
    filepath = os.path.join(IMAGES_DIR, filename)
    if os.path.exists(filepath):
        return f"/images/{filename}"
    try:
        resp, session = fetch_with_anubis(url)
        if resp.status_code == 200 and len(resp.content) > 100 and not resp.text.startswith('<'):
            with open(filepath, 'wb') as f:
                f.write(resp.content)
            return f"/images/{filename}"
    except Exception as e:
        logger.error(f"Failed to download image {url}: {e}")
    return None


def process_description_images(description):
    def replace_img(match):
        src = match.group(1)
        local = download_image(src)
        if local:
            return f'<img src="{local}" />'
        return match.group(0)

    description = re.sub(r'<img[^>]*src="([^"]+)"[^>]*/?\s*>', replace_img, description)
    return description


def fetch_feed_items(url):
    try:
        resp, session = fetch_with_anubis(url)

        if '<html' in resp.text[:500].lower():
            logger.error(f"Got HTML instead of RSS for {url}")
            return [], 'Got HTML instead of RSS (bot protection)'

        feed = feedparser.parse(resp.text)
        if not feed.entries:
            logger.warning(f"No entries in feed for {url}")
            return [], 'No entries found in feed'

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
        return items, None
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return [], str(e)


def refresh_and_cache():
    feeds = load_feeds()
    tweets = load_tweets()
    new_count = 0
    errors = []

    for feed_url in feeds:
        items, error = fetch_feed_items(feed_url)
        if error:
            errors.append({'url': feed_url, 'error': error})
        for item in items:
            tweet_id = item.get('link', '')
            if not tweet_id or tweet_id in tweets:
                continue

            profile_image = item.get('profile_image', '')
            local_avatar = download_image(profile_image) if profile_image else ''

            description = process_description_images(item.get('description', ''))

            tweets[tweet_id] = {
                'title': item.get('title', ''),
                'description': description,
                'link': tweet_id,
                'date': item.get('date', ''),
                'timestamp': item.get('timestamp', 0),
                'creator': item.get('creator', ''),
                'profile_image': local_avatar or profile_image,
                'feed_title': item.get('feed_title', ''),
                'feed_link': item.get('feed_link', ''),
                'is_retweet': item.get('is_retweet', False),
                'rt_creator': item.get('rt_creator', ''),
            }
            new_count += 1

    save_tweets(tweets)
    logger.info(f"Cached {new_count} new tweets, total {len(tweets)}")
    return new_count, len(tweets), errors


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(IMAGES_DIR, filename)


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


@app.route('/api/refresh', methods=['POST'])
def refresh():
    new_count, total, errors = refresh_and_cache()
    result = {'new_tweets': new_count, 'total': total}
    if errors:
        result['errors'] = errors
    return jsonify(result)


@app.route('/api/timeline', methods=['GET'])
def get_timeline():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)

    tweets = load_tweets()
    sorted_tweets = sorted(tweets.values(), key=lambda x: x.get('timestamp', 0), reverse=True)

    start = (page - 1) * per_page
    end = start + per_page
    page_tweets = sorted_tweets[start:end]

    return jsonify({
        'items': page_tweets,
        'total': len(sorted_tweets),
        'page': page,
        'per_page': per_page,
        'has_more': end < len(sorted_tweets)
    })


@app.route('/health')
def health():
    tweets = load_tweets()
    feeds = load_feeds()
    return jsonify({
        'status': 'ok',
        'total_tweets': len(tweets),
        'total_feeds': len(feeds),
        'cookie_cache': {k: {'expires': v['expires']} for k, v in COOKIE_CACHE.items()}
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)