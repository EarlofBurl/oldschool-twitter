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
from bs4 import BeautifulSoup

app = Flask(__name__)
DATA_DIR = '/app/data'
FEEDS_FILE = os.path.join(DATA_DIR, 'feeds.json')
TWEETS_FILE = os.path.join(DATA_DIR, 'tweets.json')
IMAGES_DIR = os.path.join(DATA_DIR, 'images')
CACHE_FILE = os.path.join(DATA_DIR, 'cache_meta.json')
COOKIE_CACHE = {}
MIN_REFRESH_INTERVAL = 600

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

if not os.path.exists(FEEDS_FILE):
    with open(FEEDS_FILE, 'w') as f:
        json.dump([], f)
if not os.path.exists(TWEETS_FILE):
    with open(TWEETS_FILE, 'w') as f:
        json.dump({}, f)
if not os.path.exists(CACHE_FILE):
    with open(CACHE_FILE, 'w') as f:
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


def load_cache_meta():
    with open(CACHE_FILE, 'r') as f:
        return json.load(f)


def save_cache_meta(meta):
    with open(CACHE_FILE, 'w') as f:
        json.dump(meta, f, indent=2)


def get_domain(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def solve_anubis(html_content, base_url, session):
    preact_match = re.search(
        r'<script\s+id="preact_info"\s+type="application/json">(.*?)</script>',
        html_content, re.DOTALL
    )
    if not preact_match:
        challenge_match = re.search(
            r'<script\s+id="anubis_challenge"\s+type="application/json">(.*?)</script>',
            html_content, re.DOTALL
        )
        if challenge_match:
            data = json.loads(challenge_match.group(1))
            challenge = data.get('challenge', {}).get('randomData', '')
            difficulty = data.get('challenge', {}).get('difficulty', data.get('rules', {}).get('difficulty', 4))
            redir = '/'
        else:
            logger.warning("No Anubis challenge found")
            return None
    else:
        preact_data = json.loads(preact_match.group(1))
        challenge = preact_data.get('challenge', '')
        difficulty = preact_data.get('difficulty', 4)
        redir = preact_data.get('redir', '/')

    logger.info(f"Solving Anubis challenge (difficulty={difficulty}) for {base_url}")

    result = hashlib.sha256(challenge.encode('utf-8')).hexdigest()

    wait_time = difficulty * 0.125 + 0.5
    logger.info(f"Waiting {wait_time:.1f}s")
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
        if '<html' in resp.text[:500].lower() and ('anubis' in resp.text.lower() or 'not a bot' in resp.text.lower()):
            logger.warning("Challenge not solved")
            return None
        logger.info("Anubis challenge solved!")
        return resp
    except Exception as e:
        logger.error(f"Anubis challenge failed: {e}")
        return None


def fetch_with_anubis(url):
    domain = get_domain(url)
    session = requests.Session()

    cached_cookies = COOKIE_CACHE.get(domain)
    if cached_cookies and cached_cookies.get('expires', 0) > time.time():
        logger.info(f"Using cached cookies for {domain}")
        for name, value in cached_cookies['cookies'].items():
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
    return url


def scrape_nitter_profile(url):
    logger.info(f"Scraping Nitter profile: {url}")
    resp, session = fetch_with_anubis(url)
    if resp.status_code != 200:
        logger.error(f"HTTP {resp.status_code} for {url}")
        return [], None

    if '<html' in resp.text[:500].lower() and ('not a bot' in resp.text.lower() or 'anubis' in resp.text.lower()):
        logger.error(f"Still bot protection for {url}")
        return [], None

    soup = BeautifulSoup(resp.text, 'html.parser')
    items = []

    profile_avatar = ''
    avatar_img = soup.select_one('.profile-avatar img')
    if avatar_img:
        profile_avatar = avatar_img.get('src', '')
        if profile_avatar and not profile_avatar.startswith('http'):
            profile_avatar = urljoin(url, profile_avatar)

    timeline = soup.select('.timeline-item')
    if not timeline:
        logger.warning(f"No timeline items found for {url}")
        return [], None

    for item in timeline:
        tweet_link_tag = item.select_one('a.tweet-link')
        if not tweet_link_tag:
            tweet_link_tag = item.select_one('.tweet-date a')
        tweet_link = ''
        if tweet_link_tag:
            tweet_link = tweet_link_tag.get('href', '')
            if tweet_link and not tweet_link.startswith('http'):
                tweet_link = urljoin(url, tweet_link)

        creator = ''
        username_tag = item.select_one('.fullname')
        if username_tag:
            creator = username_tag.get_text(strip=True)
        handle_tag = item.select_one('.username')
        if handle_tag:
            handle = handle_tag.get_text(strip=True)
            if not creator:
                creator = handle

        content_tag = item.select_one('.tweet-content')
        description = ''
        if content_tag:
            for br in content_tag.find_all('br'):
                br.replace_with('\n')
            for a in content_tag.find_all('a'):
                href = a.get('href', '')
                if href and not href.startswith('http'):
                    href = urljoin(url, href)
                a.replace_with(f'<a href="{href}" target="_blank">{a.get_text()}</a>')
            description = str(content_tag).replace('<div class="tweet-content">', '').replace('</div>', '').strip()

        date_tag = item.select_one('.tweet-date a')
        date_str = ''
        timestamp = 0
        if date_tag:
            date_str = date_tag.get('title', '')
            if not date_str:
                date_str = date_tag.get_text(strip=True)
            try:
                timestamp = datetime.strptime(date_str, '%b %d, %Y %I:%M:%S %p').timestamp()
            except ValueError:
                try:
                    timestamp = datetime.strptime(date_str, '%d %b %Y %H:%M:%S %Z').timestamp()
                except ValueError:
                    timestamp = time.time()

        is_retweet = bool(item.select_one('.retweet-header'))
        rt_creator = ''
        if is_retweet:
            rt_tag = item.select_one('.retweet-header .fullname')
            if rt_tag:
                rt_creator = rt_tag.get_text(strip=True)

        avatar = profile_avatar
        avatar_tag = item.select_one('.tweet-avatar img')
        if avatar_tag:
            avatar = avatar_tag.get('src', '')
            if avatar and not avatar.startswith('http'):
                avatar = urljoin(url, avatar)

        images = item.select('.attachment img, .tweet-content img')
        for img in images:
            src = img.get('src', '')
            if src and not src.startswith('http'):
                img['src'] = urljoin(url, src)

        quote = item.select_one('.quote')
        if quote:
            quote_text = quote.get_text(strip=True)
            description += f'\n<blockquote>{quote_text}</blockquote>'

        items.append({
            'title': description[:80] + '...' if len(description) > 80 else description,
            'description': description,
            'link': tweet_link,
            'date': date_str,
            'timestamp': timestamp or time.time(),
            'creator': creator,
            'profile_image': avatar,
            'feed_title': soup.title.string.strip() if soup.title else url,
            'feed_link': url,
            'is_retweet': is_retweet,
            'rt_creator': rt_creator,
        })

    logger.info(f"Scraped {len(items)} tweets from {url}")
    return items, profile_avatar


def fetch_rss_feed(url):
    logger.info(f"Fetching RSS feed: {url}")
    resp, session = fetch_with_anubis(url)
    if '<html' in resp.text[:500].lower():
        logger.error(f"Got HTML instead of RSS for {url}")
        return [], None

    feed = feedparser.parse(resp.text)
    if not feed.entries:
        return [], None

    profile_image = ''
    if feed.feed.get('image'):
        profile_image = feed.feed.image.get('href', '')
    elif hasattr(feed.feed, 'image'):
        profile_image = getattr(feed.feed.image, 'href', '')

    items = []
    for entry in feed.entries:
        date_str = entry.get('published', '')
        from datetime import datetime as dt
        timestamp = 0
        for fmt in ['%a, %d %b %Y %H:%M:%S %Z', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d %H:%M:%S']:
            try:
                timestamp = dt.strptime(date_str, fmt).timestamp()
                break
            except ValueError:
                pass
        if not timestamp:
            timestamp = time.time()

        creator = getattr(entry, 'author', getattr(entry, 'dc_creator', ''))
        title = entry.get('title', '')
        description = entry.get('description', entry.get('summary', ''))
        link = entry.get('link', '')

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

    return items, profile_image


def is_rss_url(url):
    return url.rstrip('/').endswith('/rss') or '/rss?' in url


def refresh_and_cache():
    feeds = load_feeds()
    tweets = load_tweets()
    cache_meta = load_cache_meta()
    new_count = 0
    errors = []

    for feed_url in feeds:
        last_refresh = cache_meta.get(feed_url, {}).get('last_refresh', 0)
        if time.time() - last_refresh < MIN_REFRESH_INTERVAL:
            logger.info(f"Skipping {feed_url} - refreshed {(time.time() - last_refresh):.0f}s ago (min {MIN_REFRESH_INTERVAL}s)")
            continue

        try:
            if is_rss_url(feed_url):
                items, profile_image = fetch_rss_feed(feed_url)
            else:
                items, profile_image = scrape_nitter_profile(feed_url)

            if not items:
                errors.append({'url': feed_url, 'error': 'No items found'})
                continue

            for item in items:
                tweet_id = item.get('link', '') or item.get('title', '')[:50]
                if not tweet_id or tweet_id in tweets:
                    continue

                profile_img = item.get('profile_image', '')
                local_avatar = download_image(profile_img) if profile_img else ''

                description = item.get('description', '')
                if not is_rss_url(feed_url):
                    description = process_description_images(description)

                tweets[tweet_id] = {
                    'title': item.get('title', ''),
                    'description': description,
                    'link': tweet_id,
                    'date': item.get('date', ''),
                    'timestamp': item.get('timestamp', 0),
                    'creator': item.get('creator', ''),
                    'profile_image': local_avatar or profile_img,
                    'feed_title': item.get('feed_title', ''),
                    'feed_link': item.get('feed_link', ''),
                    'is_retweet': item.get('is_retweet', False),
                    'rt_creator': item.get('rt_creator', ''),
                }
                new_count += 1

            cache_meta[feed_url] = {'last_refresh': time.time()}
        except Exception as e:
            logger.error(f"Error processing {feed_url}: {e}")
            errors.append({'url': feed_url, 'error': str(e)})

    save_tweets(tweets)
    save_cache_meta(cache_meta)
    logger.info(f"Cached {new_count} new tweets, total {len(tweets)}")
    return new_count, len(tweets), errors


def process_description_images(description):
    def replace_img(match):
        src = match.group(1)
        local = download_image(src)
        if local:
            return f'<img src="{local}" />'
        return match.group(0)
    description = re.sub(r'<img[^>]*src="([^"]+)"[^>]*/?\s*>', replace_img, description)
    return description


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(IMAGES_DIR, filename)


@app.route('/api/feeds', methods=['GET'])
def get_feeds():
    return jsonify(load_feeds())


@app.route('/api/feeds', methods=['POST'])
def add_feed():
    data = request.get_json()
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL required'}), 400
    if not url.startswith('http'):
        url = 'https://' + url
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
    force = request.args.get('force', '0') == '1'
    if force:
        cache_meta = load_cache_meta()
        for key in cache_meta:
            cache_meta[key]['last_refresh'] = 0
        save_cache_meta(cache_meta)
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


@app.route('/api/cache-info')
def cache_info():
    cache_meta = load_cache_meta()
    return jsonify(cache_meta)


@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'total_tweets': len(load_tweets()),
        'total_feeds': len(load_feeds()),
        'min_refresh_interval': MIN_REFRESH_INTERVAL,
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)