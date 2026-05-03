import json
import os
import re
import hashlib
import time
import logging
import threading
from datetime import datetime
from urllib.parse import quote, urlparse, urljoin
from flask import Flask, render_template, request, jsonify, send_from_directory, session, redirect, url_for
import feedparser
import requests
from bs4 import BeautifulSoup
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.urandom(32).hex()
DATA_DIR = '/app/data'
FEEDS_FILE = os.path.join(DATA_DIR, 'feeds.json')
TWEETS_FILE = os.path.join(DATA_DIR, 'tweets.json')
IMAGES_DIR = os.path.join(DATA_DIR, 'images')
CACHE_FILE = os.path.join(DATA_DIR, 'cache_meta.json')
USERS_FILE = os.path.join(DATA_DIR, 'users.json')
READSTATE_FILE = os.path.join(DATA_DIR, 'readstate.json')
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
if not os.path.exists(USERS_FILE):
    with open(USERS_FILE, 'w') as f:
        json.dump({'manuel': generate_password_hash('wrestlemania!2026')}, f)
if not os.path.exists(READSTATE_FILE):
    with open(READSTATE_FILE, 'w') as f:
        json.dump({}, f)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Cache-Control': 'no-cache',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
}


def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, 'r') as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading {path}: {e}")
    return default


def save_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_feeds():
    return load_json(FEEDS_FILE, [])


def save_feeds(feeds):
    save_json(FEEDS_FILE, feeds)


def load_tweets():
    return load_json(TWEETS_FILE, {})


def save_tweets(tweets):
    save_json(TWEETS_FILE, tweets)


def load_cache_meta():
    return load_json(CACHE_FILE, {})


def save_cache_meta(meta):
    save_json(CACHE_FILE, meta)


def load_users():
    return load_json(USERS_FILE, {})


def save_users(users):
    save_json(USERS_FILE, users)


def load_readstate():
    return load_json(READSTATE_FILE, {})


def save_readstate(data):
    save_json(READSTATE_FILE, data)


def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Not authenticated'}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated


@app.route('/login', methods=['GET'])
def login_page():
    if 'user' in session:
        return redirect(url_for('index'))
    return render_template('login.html')


@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '')
    users = load_users()
    if username in users and check_password_hash(users[username], password):
        session['user'] = username
        return jsonify({'success': True, 'user': username})
    return jsonify({'error': 'Ungültige Anmeldedaten'}), 401


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.pop('user', None)
    return jsonify({'success': True})


@app.route('/api/readstate', methods=['GET'])
@login_required
def get_readstate():
    username = session.get('user', '')
    rs = load_readstate()
    user_data = rs.get(username, {})
    return jsonify({'read_tweets': user_data.get('read_tweets', [])})


@app.route('/api/readstate', methods=['POST'])
@login_required
def update_readstate():
    username = session.get('user', '')
    data = request.get_json() or {}
    rs = load_readstate()
    rs[username] = {'read_tweets': list(set(data.get('mark_read', [])))}
    save_readstate(rs)
    return jsonify({'success': True})


@app.route('/api/register', methods=['POST'])
def api_register():
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '')
    if not username or not password or len(username) < 2:
        return jsonify({'error': 'Benutzername und Passwort erforderlich'}), 400
    users = load_users()
    if username in users:
        return jsonify({'error': 'Benutzername bereits vergeben'}), 400
    users[username] = generate_password_hash(password)
    save_users(users)
    session['user'] = username
    return jsonify({'success': True, 'user': username})


def solve_anubis(html_content, base_url, original_path, session):
    logger.info(f"Solving Anubis challenge for {base_url}{original_path}")

    preact_match = re.search(
        r'<script\s+id="preact_info"\s+type="application/json">(.*?)</script>',
        html_content, re.DOTALL
    )
    anubis_match = re.search(
        r'<script\s+id="anubis_challenge"\s+type="application/json">(.*?)</script>',
        html_content, re.DOTALL
    )

    challenge_str = ''
    difficulty = 4
    pass_path = None
    challenge_id = None

    if preact_match:
        try:
            data = json.loads(preact_match.group(1))
            challenge_str = data.get('challenge', '')
            difficulty = data.get('difficulty', 4)
            pass_path = data.get('redir', '/')
            logger.info(f"Preact method: challenge={challenge_str[:20]}..., difficulty={difficulty}")
        except json.JSONDecodeError:
            logger.error("Failed to parse preact_info JSON")
            return None

    elif anubis_match:
        try:
            data = json.loads(anubis_match.group(1))
            challenge_data = data.get('challenge', {})
            challenge_str = challenge_data.get('randomData', '')
            difficulty = data.get('rules', {}).get('difficulty', challenge_data.get('difficulty', 4))
            challenge_id = challenge_data.get('id', '')
            logger.info(f"Fast method: id={challenge_id}, difficulty={difficulty}")
        except json.JSONDecodeError:
            logger.error("Failed to parse anubis_challenge JSON")
            return None
    else:
        logger.warning("No Anubis challenge found in page")
        logger.debug(f"Page snippet: {html_content[:500]}")
        return None

    if not challenge_str:
        logger.error("No challenge string found")
        return None

    result = hashlib.sha256(challenge_str.encode('utf-8')).hexdigest()
    logger.info(f"SHA256 result: {result[:20]}...")

    wait_time = difficulty * 0.125 + 0.5
    logger.info(f"Waiting {wait_time:.1f}s (difficulty={difficulty})")
    time.sleep(wait_time)

    if pass_path:
        if '?' in pass_path:
            pass_url = f"{pass_path}&result={result}"
        else:
            pass_url = f"{pass_path}?result={result}"
    elif challenge_id:
        encoded_path = quote(original_path, safe='')
        pass_url = f"/.within.website/x/cmd/anubis/api/pass-challenge?id={challenge_id}&redir={encoded_path}&result={result}"
    else:
        logger.error("No pass path or challenge ID available")
        return None

    if not pass_url.startswith('http'):
        pass_url = urljoin(base_url, pass_url)

    logger.info(f"Pass URL: {pass_url[:100]}...")

    try:
        resp = session.get(pass_url, headers=REQUEST_HEADERS, timeout=30, allow_redirects=True)
        logger.info(f"Challenge response: status={resp.status_code}, content-type={resp.headers.get('Content-Type', '')}, len={len(resp.text)}")
        if '<html' in resp.text[:500].lower() and ('anubis' in resp.text.lower() or 'not a bot' in resp.text.lower()):
            logger.warning("Challenge not solved - still on challenge page")
            logger.debug(f"Response snippet: {resp.text[:500]}")
            return None
        logger.info("Anubis challenge solved!")
        return resp
    except Exception as e:
        logger.error(f"Failed to pass challenge: {e}")
        return None


def fetch_with_anubis(url):
    domain = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    original_path = urlparse(url).path
    if urlparse(url).query:
        original_path += '?' + urlparse(url).query

    session = requests.Session()

    cached_cookies = COOKIE_CACHE.get(domain)
    if cached_cookies and cached_cookies.get('expires', 0) > time.time():
        logger.info(f"Using cached cookies for {domain}")
        for name, value in cached_cookies['cookies'].items():
            session.cookies.set(name, value, domain=urlparse(domain).netloc)
    else:
        COOKIE_CACHE.pop(domain, None)

    resp = session.get(url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True)
    logger.info(f"Initial fetch: status={resp.status_code}, content-type={resp.headers.get('Content-Type', '')}, len={len(resp.text)}")

    is_challenge = '<html' in resp.text[:500].lower() and ('anubis' in resp.text.lower() or 'not a bot' in resp.text.lower() or 'making sure you' in resp.text.lower())

    if is_challenge:
        logger.info(f"Anubis challenge detected for {url}")
        challenge_resp = solve_anubis(resp.text, domain, original_path, session)
        if challenge_resp is not None:
            COOKIE_CACHE[domain] = {
                'cookies': {c.name: c.value for c in session.cookies},
                'expires': time.time() + 3600
            }
            if resp.headers.get('Content-Type', '').startswith('application/rss') or '<rss' in resp.text[:200]:
                return resp, session

            if challenge_resp.headers.get('Content-Type', '').startswith('application/rss') or '<rss' in challenge_resp.text[:200]:
                logger.info("Got RSS content directly from challenge redirect")
                return challenge_resp, session

            logger.info("Challenge solved, re-fetching original URL")
            resp = session.get(url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True)
            if '<html' in resp.text[:500].lower() and ('anubis' in resp.text.lower() or 'not a bot' in resp.text.lower()):
                logger.error("Still getting challenge page after solving - IP may be rate-limited")
                return None, session
        else:
            logger.warning(f"Failed to solve Anubis challenge for {url}")
            return None, session

    return resp, session


def scrape_nitter_profile(url):
    logger.info(f"Scraping Nitter profile: {url}")
    resp, session = fetch_with_anubis(url)

    if resp is None:
        return [], 'Blocked or challenge failed'

    if resp.status_code == 403 or resp.status_code == 429:
        logger.error(f"HTTP {resp.status_code} for {url} - rate limited or blocked")
        return [], f'HTTP {resp.status_code} - rate limited or blocked'

    if resp.status_code != 200:
        logger.error(f"HTTP {resp.status_code} for {url}")
        return [], f'HTTP {resp.status_code}'

    if '<html' in resp.text[:500].lower() and ('not a bot' in resp.text.lower() or 'anubis' in resp.text.lower()):
        logger.error(f"Still on challenge page for {url}")
        return [], 'Bot protection not bypassed'

    soup = BeautifulSoup(resp.text, 'html.parser')
    logger.info(f"Parsed HTML: title={soup.title.string if soup.title else 'none'}, found {len(soup.select('.timeline-item'))} timeline items, {len(soup.select('.tweet-content'))} tweet-contents")

    profile_avatar = ''
    avatar_img = soup.select_one('.profile-avatar img, .profile-card-avatar img')
    if avatar_img:
        profile_avatar = avatar_img.get('src', '')
        if profile_avatar and not profile_avatar.startswith('http'):
            profile_avatar = urljoin(url, profile_avatar)

    timeline_items = soup.select('.timeline-item')
    if not timeline_items:
        logger.warning(f"No .timeline-item found, trying alternative selectors")
        timeline_items = soup.select('[class*="timeline"]')

    items = []
    for item in timeline_items:
        tweet_link = ''
        link_tag = item.select_one('a.tweet-link, .tweet-date a, [class*="tweet-date"] a')
        if link_tag:
            tweet_link = link_tag.get('href', '')
            if tweet_link and not tweet_link.startswith('http'):
                tweet_link = urljoin(url, tweet_link)

        creator = ''
        fullname_tag = item.select_one('.fullname, .tweet-name, [class*="name"]')
        if fullname_tag:
            creator = fullname_tag.get_text(strip=True)
        username_tag = item.select_one('.username, .tweet-screen-name, [class*="username"]')
        username = ''
        if username_tag:
            username = username_tag.get_text(strip=True)
        if not creator and username:
            creator = username

        content_tag = item.select_one('.tweet-content, .tweet-body, [class*="tweet-content"]')
        description = ''
        if content_tag:
            for a_tag in content_tag.find_all('a'):
                href = a_tag.get('href', '')
                if href and not href.startswith('http'):
                    a_tag['href'] = urljoin(url, href)
                    a_tag['target'] = '_blank'
            description = str(content_tag.decode_contents())

        date_tag = item.select_one('.tweet-date a, [class*="tweet-date"] a, time')
        date_str = ''
        timestamp = int(time.time())
        if date_tag:
            date_str = date_tag.get('title', '') or date_tag.get_text(strip=True)

        is_retweet = bool(item.select_one('.retweet-header, [class*="retweet"]'))
        rt_creator = ''
        if is_retweet:
            rt_fullname = item.select_one('.retweet-header .fullname, .retweet-header [class*="name"]')
            if rt_fullname:
                rt_creator = rt_fullname.get_text(strip=True)

        avatar = profile_avatar
        avatar_tag = item.select_one('.tweet-avatar img, .avatar img, [class*="avatar"] img')
        if avatar_tag:
            avatar = avatar_tag.get('src', '')
            if avatar and not avatar.startswith('http'):
                avatar = urljoin(url, avatar)

        img_tags = item.select('.attachment img, .tweet-content img, [class*="attach"] img')
        for img in img_tags:
            src = img.get('src', '')
            if src and not src.startswith('http'):
                img['src'] = urljoin(url, src)

        if not description and not tweet_link:
            continue

        items.append({
            'title': (description[:80] + '...') if len(description) > 80 else description,
            'description': description,
            'link': tweet_link or '',
            'date': date_str,
            'timestamp': timestamp,
            'creator': creator or username or 'Unbekannt',
            'profile_image': avatar or profile_avatar,
            'feed_title': soup.title.string.strip() if soup.title and soup.title.string else url,
            'feed_link': url,
            'is_retweet': is_retweet,
            'rt_creator': rt_creator,
        })

    logger.info(f"Scraped {len(items)} tweets from {url}")
    return items, profile_avatar


def fetch_rss_feed(url):
    logger.info(f"Fetching RSS feed: {url}")
    resp, session = fetch_with_anubis(url)
    if resp is None:
        return [], 'Blocked or challenge failed'

    if '<html' in resp.text[:500].lower():
        logger.error(f"Got HTML instead of RSS for {url}")
        return [], 'Got HTML instead of RSS'

    feed = feedparser.parse(resp.text)
    if not feed.entries:
        return [], 'No entries in feed'

    profile_image = ''
    if feed.feed.get('image'):
        profile_image = feed.feed.image.get('href', '')
    elif hasattr(feed.feed, 'image'):
        profile_image = getattr(feed.feed.image, 'href', '')

    items = []
    for entry in feed.entries:
        date_str = entry.get('published', '')
        timestamp = 0
        for fmt in ['%a, %d %b %Y %H:%M:%S %Z', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d %H:%M:%S']:
            try:
                timestamp = datetime.strptime(date_str, fmt).timestamp()
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


def download_image(url, feed_url=None):
    if not url:
        return url
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
    local_path = f"/images/{filename}"
    if os.path.exists(filepath):
        if feed_url:
            _track_image(feed_url, local_path)
        return local_path
    try:
        resp, session = fetch_with_anubis(url)
        if resp and resp.status_code == 200 and len(resp.content) > 100 and not resp.text.startswith('<'):
            with open(filepath, 'wb') as f:
                f.write(resp.content)
            if feed_url:
                _track_image(feed_url, local_path)
            return local_path
    except Exception as e:
        logger.error(f"Failed to download image: {e}")
    return url


def _track_image(feed_url, local_path):
    cache_meta = load_cache_meta()
    if feed_url not in cache_meta:
        cache_meta[feed_url] = {}
    if 'images' not in cache_meta[feed_url]:
        cache_meta[feed_url]['images'] = []
    if local_path not in cache_meta[feed_url]['images']:
        cache_meta[feed_url]['images'].append(local_path)
    save_cache_meta(cache_meta)


def _delete_feed_data(feed_url):
    tweets = load_tweets()
    cache_meta = load_cache_meta()

    images_to_delete = set()
    tweet_ids_to_remove = []

    for tweet_id, tweet in tweets.items():
        if tweet.get('feed_link') == feed_url:
            if tweet.get('profile_image', '').startswith('/images/'):
                images_to_delete.add(tweet['profile_image'])
            for img_match in re.findall(r'/images/[a-f0-9]+\.\w+', tweet.get('description', '')):
                images_to_delete.add(img_match)
            tweet_ids_to_remove.append(tweet_id)

    if feed_url in cache_meta:
        feed_images = cache_meta[feed_url].get('images', [])
        images_to_delete.update(feed_images)
        del cache_meta[feed_url]

    remaining_images = set()
    for tweet_id, tweet in tweets.items():
        if tweet_id not in tweet_ids_to_remove:
            if tweet.get('profile_image', '').startswith('/images/'):
                remaining_images.add(tweet['profile_image'])
            for img_match in re.findall(r'/images/[a-f0-9]+\.\w+', tweet.get('description', '')):
                remaining_images.add(img_match)
    for other_feed, feed_data in cache_meta.items():
        for img in feed_data.get('images', []):
            remaining_images.add(img)

    images_to_delete -= remaining_images

    for image_path in images_to_delete:
        filename = image_path.replace('/images/', '')
        filepath = os.path.join(IMAGES_DIR, filename)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
                logger.info(f"Deleted image: {filename}")
            except Exception as e:
                logger.error(f"Failed to delete image {filename}: {e}")

    for tweet_id in tweet_ids_to_remove:
        del tweets[tweet_id]

    save_tweets(tweets)
    save_cache_meta(cache_meta)

    logger.info(f"Deleted {len(tweet_ids_to_remove)} tweets and {len(images_to_delete)} images for feed {feed_url}")
    return len(images_to_delete)


def process_description_images(description, feed_url=None):
    def replace_img(match):
        src = match.group(1)
        local = download_image(src, feed_url)
        if local and local.startswith('/images/'):
            return f'<img src="{local}" />'
        return match.group(0)
    description = re.sub(r'<img[^>]*src="([^"]+)"[^>]*/?\s*>', replace_img, description)
    return description


def refresh_and_cache():
    try:
        feeds = load_feeds()
        tweets = load_tweets()
        cache_meta = load_cache_meta()
    except Exception as e:
        logger.error(f"Error loading data files: {e}", exc_info=True)
        return 0, 0, [{'url': 'local', 'error': f'Data load error: {str(e)}'}]

    new_count = 0
    errors = []

    for feed_url in feeds:
        try:
            last_refresh = cache_meta.get(feed_url, {}).get('last_refresh', 0)
            if time.time() - last_refresh < MIN_REFRESH_INTERVAL:
                logger.info(f"Skipping {feed_url} - refreshed {(time.time() - last_refresh):.0f}s ago")
                continue

            if is_rss_url(feed_url):
                items, profile_image = fetch_rss_feed(feed_url)
            else:
                items, profile_image = scrape_nitter_profile(feed_url)

            if not items:
                if profile_image is None:
                    errors.append({'url': feed_url, 'error': 'Blocked or request failed'})
                else:
                    errors.append({'url': feed_url, 'error': 'No items found'})
                cache_meta[feed_url] = {'last_refresh': time.time()}
                continue

            for item in items:
                tweet_id = item.get('link', '') or hashlib.md5((item.get('title', '') + item.get('date', '')).encode()).hexdigest()
                if tweet_id in tweets:
                    continue

                profile_img = item.get('profile_image', '')
                local_avatar = download_image(profile_img, feed_url) if profile_img else ''

                description = item.get('description', '')
                if is_rss_url(feed_url):
                    description = process_description_images(description, feed_url)

                tweets[tweet_id] = {
                    'title': item.get('title', ''),
                    'description': description,
                    'link': item.get('link', tweet_id),
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
            logger.error(f"Error processing {feed_url}: {e}", exc_info=True)
            errors.append({'url': feed_url, 'error': str(e)})

    try:
        save_tweets(tweets)
        save_cache_meta(cache_meta)
    except Exception as e:
        logger.error(f"Error saving data: {e}", exc_info=True)
        errors.append({'url': 'local', 'error': f'Data save error: {str(e)}'})

    logger.info(f"Cached {new_count} new tweets, total {len(tweets)}")
    return new_count, len(tweets), errors


def extract_hashtags(text):
    clean = re.sub(r'<[^>]+>', ' ', text)
    return [('#' + t).lower() for t in re.findall(r'#(\w+)', clean)]


@app.route('/api/hashtags')
@login_required
def get_hashtags():
    feed_url = request.args.get('feed_url', '').strip()
    tweets = load_tweets()
    tag_counts = {}
    four_days_ago = time.time() - 4 * 86400
    for tweet in tweets.values():
        if feed_url and tweet.get('feed_link') != feed_url:
            continue
        if tweet.get('timestamp', 0) < four_days_ago:
            continue
        for tag in extract_hashtags(tweet.get('description', '') + ' ' + tweet.get('title', '')):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    return jsonify({'hashtags': [{'tag': t, 'count': c} for t, c in sorted_tags]})


@app.route('/')
@login_required
def index():
    return render_template('index.html')


@app.route('/profile')
@login_required
def profile():
    return render_template('profile.html')


@app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(IMAGES_DIR, filename)


@app.route('/api/feeds', methods=['GET'])
@login_required
def get_feeds():
    return jsonify(load_feeds())


@app.route('/api/feeds/profiles', methods=['GET'])
@login_required
def get_feed_profiles():
    feeds = load_feeds()
    tweets = load_tweets()
    profiles = {}
    for tweet in tweets.values():
        feed_link = tweet.get('feed_link', '')
        if not feed_link:
            continue
        if feed_link not in profiles:
            profiles[feed_link] = {
                'feed_link': feed_link,
                'feed_title': tweet.get('feed_title', feed_link),
                'creator': tweet.get('creator', ''),
                'profile_image': tweet.get('profile_image', ''),
                'tweet_count': 0,
                'latest_timestamp': 0,
                'is_subscribed': feed_link in feeds,
            }
        profiles[feed_link]['tweet_count'] += 1
        if tweet.get('timestamp', 0) > profiles[feed_link]['latest_timestamp']:
            profiles[feed_link]['latest_timestamp'] = tweet.get('timestamp', 0)
            profiles[feed_link]['creator'] = tweet.get('creator', profiles[feed_link]['creator'])
            profiles[feed_link]['profile_image'] = tweet.get('profile_image', profiles[feed_link]['profile_image'])
            profiles[feed_link]['feed_title'] = tweet.get('feed_title', profiles[feed_link]['feed_title'])
    return jsonify(list(profiles.values()))


@app.route('/api/search')
@login_required
def search():
    q = request.args.get('q', '').strip().lower()
    if not q:
        return jsonify({'tweets': [], 'profiles': []})

    tweets = load_tweets()
    feeds = load_feeds()

    matched_tweets = []
    for tweet in tweets.values():
        creator = tweet.get('creator', '').lower()
        title = tweet.get('title', '').lower()
        feed_title = tweet.get('feed_title', '').lower()
        description = tweet.get('description', '').lower()
        if q in creator or q in title or q in feed_title or q in description:
            matched_tweets.append(tweet)

    matched_tweets.sort(key=lambda x: x.get('timestamp', 0), reverse=True)

    profiles = {}
    for tweet in matched_tweets:
        feed_link = tweet.get('feed_link', '')
        if feed_link and feed_link not in profiles:
            profiles[feed_link] = {
                'feed_link': feed_link,
                'feed_title': tweet.get('feed_title', feed_link),
                'creator': tweet.get('creator', ''),
                'profile_image': tweet.get('profile_image', ''),
                'tweet_count': sum(1 for t in tweets.values() if t.get('feed_link') == feed_link),
                'is_subscribed': feed_link in feeds,
            }

    return jsonify({
        'tweets': matched_tweets[:50],
        'profiles': list(profiles.values()),
        'query': q,
        'total_tweets': len(matched_tweets),
    })


@app.route('/api/profile/<path:feed_url>')
@login_required
def get_profile(feed_url):
    if not feed_url.startswith('http'):
        feed_url = 'https://' + feed_url
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    hashtag = request.args.get('hashtag', '').strip().lower()

    tweets = load_tweets()
    feeds = load_feeds()

    feed_tweets = [t for t in tweets.values() if t.get('feed_link') == feed_url]

    if hashtag:
        if not hashtag.startswith('#'):
            hashtag = '#' + hashtag
        feed_tweets = [t for t in feed_tweets if hashtag in extract_hashtags(t.get('description', '') + ' ' + t.get('title', ''))]

    feed_tweets.sort(key=lambda x: x.get('timestamp', 0), reverse=True)

    creator = ''
    profile_image = ''
    feed_title = feed_url
    if feed_tweets:
        creator = feed_tweets[0].get('creator', '')
        profile_image = feed_tweets[0].get('profile_image', '')
        feed_title = feed_tweets[0].get('feed_title', feed_url)

    start = (page - 1) * per_page
    end = start + per_page
    page_tweets = feed_tweets[start:end]

    return jsonify({
        'items': page_tweets,
        'total': len(feed_tweets),
        'page': page,
        'per_page': per_page,
        'has_more': end < len(feed_tweets),
        'feed_url': feed_url,
        'feed_title': feed_title,
        'creator': creator,
        'profile_image': profile_image,
        'is_subscribed': feed_url in feeds,
    })


@app.route('/api/feeds', methods=['POST'])
@login_required
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
@login_required
def remove_feed():
    data = request.get_json()
    url = data.get('url', '').strip()
    feeds = load_feeds()
    if url in feeds:
        feeds.remove(url)
        save_feeds(feeds)
        images_deleted = _delete_feed_data(url)
        tweets = load_tweets()
        return jsonify({'feeds': feeds, 'deleted_images': images_deleted, 'remaining_tweets': len(tweets)})
    return jsonify({'feeds': feeds})


@app.route('/api/refresh', methods=['POST'])
@login_required
def refresh():
    try:
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
    except Exception as e:
        logger.error(f"Refresh endpoint error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/timeline', methods=['GET'])
@login_required
def get_timeline():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    hashtag = request.args.get('hashtag', '').strip().lower()

    tweets = load_tweets()
    sorted_tweets = sorted(tweets.values(), key=lambda x: x.get('timestamp', 0), reverse=True)

    if hashtag:
        if not hashtag.startswith('#'):
            hashtag = '#' + hashtag
        sorted_tweets = [t for t in sorted_tweets if hashtag in extract_hashtags(t.get('description', '') + ' ' + t.get('title', ''))]

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


@app.route('/api/debug/<path:url>')
@login_required
def debug_fetch(url):
    if not url.startswith('http'):
        url = 'https://' + url
    try:
        resp, session = fetch_with_anubis(url)
        if resp is None:
            return jsonify({'error': 'Blocked or challenge failed', 'url': url})
        return jsonify({
            'url': url,
            'status_code': resp.status_code,
            'content_type': resp.headers.get('Content-Type', ''),
            'content_length': len(resp.text),
            'is_html': '<html' in resp.text[:500].lower(),
            'is_rss': '<rss' in resp.text[:200].lower(),
            'first_500': resp.text[:500],
            'cookies': {c.name: c.value for c in session.cookies},
        })
    except Exception as e:
        return jsonify({'error': str(e), 'url': url})


@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'total_tweets': len(load_tweets()),
        'total_feeds': len(load_feeds()),
        'min_refresh_interval': MIN_REFRESH_INTERVAL,
        'cookie_cache': {k: {'expires': v['expires']} for k, v in COOKIE_CACHE.items()},
    })


_background_refresh_thread = None


def start_background_refresh():
    def run():
        while True:
            time.sleep(15 * 60)
            logging.info("Background refresh triggered")
            try:
                refresh_and_cache()
            except Exception as e:
                logging.error(f"Background refresh failed: {e}")
    t = threading.Thread(target=run, daemon=True)
    t.start()
    logging.info("Background refresh thread started")


if __name__ == '__main__':
    start_background_refresh()
    app.run(host='0.0.0.0', port=5000, threaded=True)