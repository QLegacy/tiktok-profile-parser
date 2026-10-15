"""
Выкачка профиля TikTok (v5: атомарная запись, стоп при нехватке места, видео ≤360p).

Источники данных:
  - профиль:        страница tiktok.com/@ник (JSON внутри HTML)
  - список видео:   yt-dlp --flat-playlist, запасной вариант tikwm user/posts
  - данные видео:   tikwm /api/ (без hd), запасной вариант yt-dlp -J
  - видеофайл:      yt-dlp с лимитом высоты (VIDEO_MAX_HEIGHT), запасной вариант tikwm play
  - комментарии:    tikwm comment/* (если закрыт Cloudflare -> пропускаем)
  - подписчики:     tikwm user/followers|following (скорее всего закрыто)

Структура:
  <username>/
    userpic_YYYY-MM-DD.jpg|png|webp|gif
    info_YYYY-MM-DD.json
    followers_YYYY-MM-DD.jsonl / following_YYYY-MM-DD.jsonl   (опц.)
    <video_id>/
      video.mp4 | pic1.jpg ...
      sound.mp3
      video_info.json
      comments.jsonl     одна строка = комментарий верхнего уровня с вложенными ответами
      comment_media/
"""
import os
import re
import sys
import json
import time
import errno
import shutil
import hashlib
import datetime
import threading
import subprocess
import requests

from main import HEADERS, _rate_limit_tikwm, log_error, _guess_ext
import atexit
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

FF_BIN = "/data/data/com.termux/files/usr/bin/firefox"
GECKO = "/data/data/com.termux/files/usr/bin/geckodriver"
BROWSER_RECYCLE_EVERY = 40      # перезапуск браузера раз в N профилей (память телефона)
_DRV = {'d': None, 'n': 0}
API = "https://www.tikwm.com/api"
TODAY = datetime.date.today().isoformat()

# --- качество видео
# 'ytdlp'  -> сначала yt-dlp с лимитом высоты, если не вышло -> tikwm "play" (SD, без hd)
# 'tikwm'  -> сначала tikwm "play" (SD, быстрее), если не вышло -> yt-dlp с лимитом высоты
VIDEO_MODE = 'ytdlp'
VIDEO_MAX_HEIGHT = 360

CT_EXT = {
    'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp', 'image/gif': 'gif',
    'audio/mpeg': 'mp3', 'audio/mp4': 'm4a', 'video/mp4': 'mp4',
}
KNOWN_EXT = ('jpg', 'jpeg', 'png', 'webp', 'gif', 'mp3', 'm4a', 'mp4')
MEDIA_KEYS = ('image_list', 'sticker', 'images', 'audio', 'voice')

STATE = {'comments_dead': False}


class ApiUnavailable(Exception):
    """Первая страница эндпоинта не отдалась (Cloudflare / закрыто / ошибка)."""


# ---------------------------------------------------------------- console / progress bar
class Console:
    """Лог-строки печатаются сверху, строка прогресса остаётся внизу и тикает раз в секунду."""

    def __init__(self):
        self.lock = threading.RLock()
        self.tty = sys.stdout.isatty()
        self.t0 = time.time()
        self.pos = None
        self.total = None
        self._stop = threading.Event()
        self._thread = None

    def _bar_text(self):
        el = int(time.time() - self.t0)
        clock = f"{el // 3600:02d}:{el % 3600 // 60:02d}:{el % 60:02d}"
        head = f"{clock} video {self.pos} ["
        tail = f"] {self.total}"
        cols = shutil.get_terminal_size((60, 20)).columns
        width = max(10, min(40, cols - len(head) - len(tail) - 2))
        filled = max(1, min(width, int(width * self.pos / self.total))) if self.total else 1
        return head + '=' * filled + '|' + '-' * (width - filled) + tail

    def _draw(self):
        if self.tty and self.pos is not None:
            sys.stdout.write('\r\033[K' + self._bar_text())
            sys.stdout.flush()

    def _clear(self):
        if self.tty and self.pos is not None:
            sys.stdout.write('\r\033[K')

    def log(self, msg=''):
        with self.lock:
            self._clear()
            sys.stdout.write(msg + '\n')
            self._draw()
            sys.stdout.flush()

    def start_bar(self, total):
        with self.lock:
            self.total = max(1, total)
            self.pos = 0
            self._stop.clear()
        if self.tty:
            self._thread = threading.Thread(target=self._ticker, daemon=True)
            self._thread.start()

    def set_pos(self, pos):
        with self.lock:
            self.pos = pos
            self._draw()

    def _ticker(self):
        while not self._stop.wait(1.0):
            with self.lock:
                self._draw()

    def stop(self):
        self._stop.set()
        with self.lock:
            self._clear()
            self.pos = None
            sys.stdout.flush()


con = Console()
log = con.log


def fmt_dur(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    return f"{int(s // 60)}m{int(s % 60):02d}s"


def _clip(text, n=100) -> str:
    t = ' '.join(str(text or '').split())
    return t if len(t) <= n else t[:n] + '…'


def _fsize(path) -> str:
    try:
        b = os.path.getsize(path)
    except OSError:
        return '?'
    return f"{b / 1024 / 1024:.1f} MB" if b >= 1024 * 1024 else f"{b / 1024:.0f} KB"


# ---------------------------------------------------------------- disk space guard
def _check_disk(e):
    """Если это Errno 28 (нет места) — сразу останавливаем весь процесс."""
    if isinstance(e, OSError) and e.errno == errno.ENOSPC:
        log("\n[!!!] Нет места на диске (Errno 28). Останавливаюсь. "
            "Освободи место, запусти repair.py и повтори запуск.")
        sys.exit(2)


def _rm(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- helpers
def _abs(url: str) -> str:
    return url if url.startswith('http') else f"https://www.tikwm.com{url}"


def _iso(ts):
    try:
        return datetime.datetime.fromtimestamp(int(ts)).isoformat() if ts else None
    except (ValueError, TypeError, OSError):
        return None


def _dump(path, obj):
    """Атомарная запись json: сначала .tmp, потом replace."""
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        _rm(tmp)
        raise


def api_get(path: str, **params):
    """GET к tikwm. Возвращает data или None. На не-JSON (Cloudflare) не ретраит."""
    url = f"{API}/{path}" if path else f"{API}/"
    shown = path or '/'
    for attempt in range(3):
        try:
            _rate_limit_tikwm()
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
            try:
                j = r.json()
            except ValueError:
                log_error(f"[tikwm:{shown}] не JSON (HTTP {r.status_code}, похоже на Cloudflare)")
                return None
            if j.get('code') == 0:
                return j.get('data')
            msg = str(j.get('msg', ''))
            if 'limit' in msg.lower():
                log(f"    [tikwm] лимит запросов, жду {3 * (attempt + 1)}s...")
                time.sleep(3 * (attempt + 1))
                continue
            log_error(f"[tikwm:{shown}] {msg} {params}")
            return None
        except Exception as e:
            log_error(f"[tikwm:{shown}] исключение: {e}")
            time.sleep(2)
    return None


def iter_pages(path: str, list_key: str, cursor_param='cursor', **params):
    """Генератор страниц (списков). Память O(страница).
    ApiUnavailable, если не удалась самая первая страница; дальше сбой просто завершает итерацию."""
    cursor, first = 0, True
    while True:
        data = api_get(path, **{cursor_param: cursor}, **params)
        if not data:
            if first:
                raise ApiUnavailable(path)
            return
        first = False
        items = data.get(list_key) or []
        if items:
            yield items
        if not items or not data.get('hasMore'):
            return
        new_cursor = data.get('cursor', data.get('time'))
        if new_cursor is None or new_cursor == cursor:
            return
        cursor = new_cursor


def paginate(path: str, list_key: str, max_items=None, cursor_param='cursor', **params):
    """Собирает всё в список (для небольших выборок). None, если первая страница не отдалась."""
    out = []
    try:
        for items in iter_pages(path, list_key, cursor_param=cursor_param, **params):
            out.extend(items)
            if max_items and len(out) >= max_items:
                break
    except ApiUnavailable:
        return None
    return out[:max_items] if max_items else out


def save_url(url: str, base: str):
    """Качает url в base.<ext> (расширение по Content-Type) через временный .part.
    Возвращает имя файла или None. При нехватке места завершает процесс."""
    os.makedirs(os.path.dirname(base) or '.', exist_ok=True)
    for e in KNOWN_EXT:
        if os.path.exists(f"{base}.{e}"):
            return os.path.basename(f"{base}.{e}")
    tmp = None
    try:
        with requests.get(_abs(url), headers=HEADERS, timeout=60, stream=True) as r:
            if r.status_code != 200:
                log_error(f"HTTP {r.status_code}: {url}")
                return None
            ct = r.headers.get('Content-Type', '').split(';')[0].strip().lower()
            ext = CT_EXT.get(ct) or _guess_ext(url)
            path = f"{base}.{ext}"
            tmp = path + '.part'
            with open(tmp, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        os.replace(tmp, path)
        return os.path.basename(path)
    except BaseException as e:
        _rm(tmp)
        if isinstance(e, Exception):
            _check_disk(e)
            log_error(f"Ошибка скачивания {url}: {e}")
            return None
        raise   # KeyboardInterrupt / SystemExit


def _sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def find_urls(obj):
    if isinstance(obj, str):
        if obj.startswith('http'):
            yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from find_urls(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from find_urls(v)


# ---------------------------------------------------------------- profile page
def _close_driver():
    d = _DRV['d']
    _DRV['d'] = None
    if d:
        try:
            d.quit()
        except Exception:
            pass

atexit.register(_close_driver)


def _driver():
    if _DRV['d'] is not None and _DRV['n'] >= BROWSER_RECYCLE_EVERY:
        _close_driver()
    if _DRV['d'] is None:
        o = Options()
        o.add_argument("-headless")
        o.binary_location = FF_BIN
        d = webdriver.Firefox(options=o, service=Service(GECKO))
        d.set_page_load_timeout(45)
        _DRV['d'], _DRV['n'] = d, 0
    _DRV['n'] += 1
    return _DRV['d']


_JS_GET = ("var e=document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');"
           "return e ? e.textContent : null;")


def _page_json(username: str, wait=30):
    d = _driver()
    d.get(f"https://www.tiktok.com/@{username}")
    end = time.time() + wait
    while time.time() < end:
        try:
            raw = d.execute_script(_JS_GET)
            if raw:
                return raw
        except Exception:
            pass                      # страница перезагружается после челленджа
        time.sleep(1)
    return None


def fetch_profile_page(username: str):
    raw = None
    for attempt in range(2):
        try:
            raw = _page_json(username)
        except Exception as e:
            log_error(f"Браузер {username}: {e}")
            _close_driver()           # сломанный браузер пересоздаём
        if raw:
            break
        if attempt == 0:
            log("    [страница] данных нет, перезапускаю браузер и пробую ещё раз")
            _close_driver()
            time.sleep(5)
    if not raw:
        log_error(f"Профиль {username}: нет rehydration-данных (браузер)")
        return None
    try:
        detail = json.loads(raw)['__DEFAULT_SCOPE__'].get('webapp.user-detail') or {}
    except (ValueError, KeyError) as e:
        log_error(f"Профиль {username}: не разобрать JSON ({e})")
        return None
    code = detail.get('statusCode')
    if code not in (0, None):
        log(f"    [страница] statusCode={code} {detail.get('statusMsg', '')}")
    return detail.get('userInfo') or None

# ---------------------------------------------------------------- video list
def list_videos_ytdlp(username: str, sec_uid: str = None) -> list:
    if shutil.which('yt-dlp') is None:
        return []
    target = f"tiktokuser:{sec_uid}" if sec_uid else f"https://www.tiktok.com/@{username}"
    cmd = ['yt-dlp', '--flat-playlist', '-J', '--no-warnings', target]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if res.stderr.strip():
            log(f"    [yt-dlp] {res.stderr.strip().splitlines()[-1]}")
        if not res.stdout.strip():
            return []
        j = json.loads(res.stdout)
        if not isinstance(j, dict):
            return []
        return [str(e['id']) for e in (j.get('entries') or []) if e and e.get('id')]
    except Exception as e:
        log_error(f"yt-dlp список {username}: {e}")
        return []


def list_videos_tikwm(username: str) -> list:
    items = paginate('user/posts', 'videos', unique_id=username, count=35)
    return [str(v.get('video_id') or v.get('id')) for v in (items or [])]


# ---------------------------------------------------------------- video data
def _from_ytdlp(url: str):
    """Метаданные через yt-dlp в формате, похожем на ответ tikwm."""
    if shutil.which('yt-dlp') is None:
        return None
    try:
        res = subprocess.run(['yt-dlp', '-J', '--no-download', '--no-warnings', url],
                             capture_output=True, text=True, timeout=120)
        if res.returncode != 0 or not res.stdout.strip():
            return None
        j = json.loads(res.stdout)
        return {
            'title': j.get('description') or j.get('title') or '',
            'create_time': j.get('timestamp'),
            'duration': j.get('duration'),
            'play_count': j.get('view_count'),
            'digg_count': j.get('like_count'),
            'comment_count': j.get('comment_count'),
            'share_count': j.get('repost_count'),
            'collect_count': j.get('save_count'),
            'music_info': {'title': j.get('track'), 'author': ', '.join(j.get('artists') or []) or None},
        }
    except Exception as e:
        log_error(f"yt-dlp -J {url}: {e}")
        return None


def _download_ytdlp_capped(url: str, folder: str) -> list:
    """Качает видео через yt-dlp с лимитом высоты VIDEO_MAX_HEIGHT.
    Если форматов <= лимита нет, берёт самый лёгкий из доступных.
    Возвращает список имён файлов (пусто при неудаче)."""
    if shutil.which('yt-dlp') is None:
        return []
    os.makedirs(folder, exist_ok=True)
    for fn in os.listdir(folder):
        if fn.startswith('video.') and not fn.endswith(('.part', '.ytdl', '.tmp')):
            return [fn]
    h = VIDEO_MAX_HEIGHT
    fmt = f"b[height<={h}]/bv*[height<={h}]+ba/wv*+ba/w"
    cmd = ['yt-dlp', '-f', fmt, '--no-playlist', '--no-warnings', '--socket-timeout', '15',
           '--merge-output-format', 'mp4', '-o', os.path.join(folder, 'video.%(ext)s'), url]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as e:
        log_error(f"yt-dlp видео {url}: {e}")
        return []
    err = (res.stderr or '')
    if 'No space left on device' in err:
        _check_disk(OSError(errno.ENOSPC, 'No space left on device'))
    if res.returncode != 0:
        log_error(f"yt-dlp видео {url}: {err.strip().splitlines()[-1] if err.strip() else 'code != 0'}")
        for fn in os.listdir(folder):          # чистим недокачанное
            if fn.startswith('video.'):
                _rm(os.path.join(folder, fn))
        return []
    return [fn for fn in os.listdir(folder)
            if fn.startswith('video.') and not fn.endswith(('.part', '.ytdl', '.tmp'))]


# ---------------------------------------------------------------- comments
def normalize_comment(c: dict, media_dir: str, keep_raw=False) -> dict:
    u = c.get('user') or {}
    uid = str(u.get('id') or u.get('uid') or '')
    uname = u.get('unique_id') or u.get('uniqueId')

    avatar_file = None
    if u.get('avatar') and uid:
        avatar_file = save_url(u['avatar'], os.path.join(media_dir, 'avatars', uid))

    cid = str(c.get('id'))
    media = {}
    for key in MEDIA_KEYS:
        if c.get(key):
            urls = list(dict.fromkeys(find_urls(c[key])))
            if urls:
                f = save_url(urls[0], os.path.join(media_dir, f"{cid}_{key}"))
                if f:
                    media[key] = {'url': urls[0], 'file': f}

    reply_to = c.get('reply_to_reply_id') or c.get('reply_id')
    node = {
        'id': cid,
        'text': c.get('text'),
        'created': _iso(c.get('create_time')),
        'likes': c.get('digg_count'),
        'reply_total': c.get('reply_total'),
        'reply_to': str(reply_to) if reply_to and str(reply_to) != '0' else None,
        'user': {
            'id': uid,
            'username': uname,
            'nickname': u.get('nickname'),
            'profile_url': f"https://www.tiktok.com/@{uname}" if uname else None,
            'avatar_url': u.get('avatar'),
            'avatar_file': (f"avatars/{avatar_file}" if avatar_file else None),
        },
        'media': media,
        'replies': [],
    }
    if keep_raw:
        node['raw'] = c
    return node


def build_reply_tree(flat: list) -> list:
    by_id = {r['id']: r for r in flat}
    top = []
    for r in flat:
        p = r.get('reply_to')
        if p and p in by_id and p != r['id']:
            by_id[p]['replies'].append(r)
        else:
            top.append(r)
    return top


def _who(node: dict) -> str:
    u = node['user']
    name = u.get('nickname') or '?'
    return f"{name} (@{u['username']})" if u.get('username') else name


def _media_tag(node: dict) -> str:
    return f" +медиа:{','.join(node['media'])}" if node.get('media') else ''


def _load_done(path: str) -> set:
    """Читает уже сохранённые id из comments.jsonl; обрезает недописанную последнюю строку."""
    done = set()
    if not os.path.exists(path):
        return done
    good_end = 0
    with open(path, 'rb') as f:
        for line in f:
            if not line.endswith(b'\n'):
                break
            try:
                done.add(str(json.loads(line)['id']))
            except Exception:
                break
            good_end += len(line)
    os.truncate(path, good_end)
    return done


def fetch_comments(video_id: str, folder: str, declared=None, keep_raw=False):
    """Пишет comments.jsonl потоком (с докачкой). Возвращает число комментариев или None, если API закрыт."""
    media_dir = os.path.join(folder, 'comment_media')
    path = os.path.join(folder, 'comments.jsonl')
    done = _load_done(path)
    if done:
        log(f"    [комменты] продолжаю: уже сохранено {len(done)}")

    t_all = time.time()
    t_page = time.time()
    page = 0
    replies_total = 0
    of = f"/{declared}" if declared else ""

    try:
        with open(path, 'a', encoding='utf-8') as out:
            for items in iter_pages('comment/list', 'comments', url=video_id, count=50):
                page += 1
                log(f"    [комменты] стр. {page}: +{len(items)} (сохранено {len(done)}{of}) "
                    f"— {fmt_dur(time.time() - t_page)}")
                for c in items:
                    cid = str(c.get('id'))
                    if cid in done:
                        continue
                    t_c = time.time()
                    node = normalize_comment(c, media_dir, keep_raw)
                    n = len(done) + 1
                    log(f"    #{n} {_who(node)}: {_clip(node['text'])}{_media_tag(node)} "
                        f"[♥{node['likes']}, ответов {node['reply_total']}] — {fmt_dur(time.time() - t_c)}")

                    if (c.get('reply_total') or 0) > 0:
                        t_r = time.time()
                        replies = paginate('comment/reply', 'comments',
                                           video_id=video_id, comment_id=c.get('id'), count=50) or []
                        flat = []
                        for r in replies:
                            t_rr = time.time()
                            rn = normalize_comment(r, media_dir, keep_raw)
                            flat.append(rn)
                            log(f"        ↳ {_who(rn)}: {_clip(rn['text'])}{_media_tag(rn)} "
                                f"[♥{rn['likes']}] — {fmt_dur(time.time() - t_rr)}")
                        replies_total += len(flat)
                        node['replies'] = build_reply_tree(flat)
                        log(f"        [ответы к #{n}] {len(flat)} шт — {fmt_dur(time.time() - t_r)}")

                    out.write(json.dumps(node, ensure_ascii=False) + '\n')
                    out.flush()
                    done.add(cid)
                t_page = time.time()
    except ApiUnavailable:
        if not done:
            if os.path.exists(path) and os.path.getsize(path) == 0:
                os.remove(path)
            return None
        log("    [комменты] API не отвечает, оставляю то, что уже сохранено")

    log(f"    [комменты] готово: {len(done)} + ответов {replies_total} (в этом запуске) "
        f"— {fmt_dur(time.time() - t_all)}")
    return len(done)


# ---------------------------------------------------------------- followers / following
def fetch_follow(kind: str, key: str, user_id, root: str):
    """Потоково пишет {kind}_ДАТА.jsonl. Возвращает число записей или None, если недоступно."""
    path = os.path.join(root, f"{kind}_{TODAY}.jsonl")
    t = time.time()
    n = page = 0
    try:
        with open(path, 'w', encoding='utf-8') as out:
            for items in iter_pages(f'user/{kind}', key, cursor_param='time', user_id=user_id, count=50):
                page += 1
                for u in items:
                    out.write(json.dumps(u, ensure_ascii=False) + '\n')
                    n += 1
                out.flush()
                log(f"    [{kind}] стр. {page}: всего {n} — {fmt_dur(time.time() - t)}")
    except ApiUnavailable:
        if os.path.exists(path):
            os.remove(path)
        log(f"    [{kind}] недоступно (скорее всего Cloudflare или список скрыт)")
        return None
    log(f"    [{kind}] готово: {n} шт — {fmt_dur(time.time() - t)}")
    return n

# ---------------------------------------------------------------- liked video

def fetch_likes(user_id, root: str, declared=None, keep_raw=False):
    """Лайкнутые видео -> likes_ДАТА.jsonl (только метаданные, файлы не качаем).
    Возвращает число записей или None, если API недоступен."""
    path = os.path.join(root, f"likes_{TODAY}.jsonl")
    tmp = path + '.tmp'
    t = time.time()
    n = page = 0
    try:
        with open(tmp, 'w', encoding='utf-8') as out:
            for items in iter_pages('user/favorite', 'videos', user_id=user_id, count=50):
                page += 1
                for v in items:
                    a = v.get('author') or {}
                    uname = a.get('unique_id')
                    vid = str(v.get('video_id') or v.get('id'))
                    rec = {
                        'video_id': vid,
                        'url': f"https://www.tiktok.com/@{uname or '_'}/video/{vid}",
                        'description': v.get('title'),
                        'created': _iso(v.get('create_time')),
                        'author': {'id': a.get('id'), 'username': uname, 'nickname': a.get('nickname')},
                        'is_top': v.get('is_top'),
                        'stats': {
                            'views': v.get('play_count'), 'likes': v.get('digg_count'),
                            'comments': v.get('comment_count'), 'shares': v.get('share_count'),
                            'saves': v.get('collect_count'),
                        },
                        'is_photo': bool(v.get('images')),
                    }
                    if keep_raw:
                        rec['raw'] = v
                    out.write(json.dumps(rec, ensure_ascii=False) + '\n')
                    n += 1
                out.flush()
                log(f"    [лайки] стр. {page}: всего {n} — {fmt_dur(time.time() - t)}")
    except ApiUnavailable:
        _rm(tmp)
        log("    [лайки] недоступно (Cloudflare или список скрыт)")
        return None
    except BaseException:
        _rm(tmp)
        raise
    if n == 0:
        _rm(tmp)
        log("    [лайки] пусто")
        return 0
    os.replace(tmp, path)
    note = f" (по странице: {declared})" if declared else ""
    log(f"    [лайки] готово: {n} шт{note} — {fmt_dur(time.time() - t)}")
    return n

# ---------------------------------------------------------------- one video
def process_video(vid: str, root: str, username: str, with_comments: bool, keep_raw=False):
    t_all = time.time()
    folder = os.path.join(root, vid)
    info_path = os.path.join(folder, 'video_info.json')
    video_url = f"https://www.tiktok.com/@{username}/video/{vid}"

    if os.path.exists(info_path):
        try:
            with open(info_path, encoding='utf-8') as f:
                d = json.load(f)
            if d.get('complete') and not (with_comments and d.get('comments_status') in ('disabled', 'unavailable')):
                log("    [пропуск] уже скачано")
                return 'skipped'
        except Exception:
            pass

    # --- данные видео (без hd=1, чтобы не тянуть тяжёлые ссылки)
    t = time.time()
    log("    [данные] запрос к tikwm...")
    v = api_get('', url=video_url)
    source = 'tikwm'
    if not v:
        log("    [данные] tikwm не ответил, пробуем yt-dlp...")
        v = _from_ytdlp(video_url)
        source = 'yt-dlp'
    if not v:
        log(f"    [данные] НЕ УДАЛОСЬ — {fmt_dur(time.time() - t)}")
        log_error(f"Нет данных по видео {vid}")
        return 'failed'
    log(f"    [данные] ок ({source}) — {fmt_dur(time.time() - t)}")

    os.makedirs(folder, exist_ok=True)
    title = v.get('title') or ''
    log(f"    [описание] {_clip(title)}")
    log(f"    [статы] просмотры {v.get('play_count')} | ♥ {v.get('digg_count')} | "
        f"комменты {v.get('comment_count')} | репосты {v.get('share_count')} | сохранения {v.get('collect_count')}")

    files = []

    # --- медиа
    if v.get('images'):
        imgs = v['images']
        t = time.time()
        log(f"    [фото] карусель, {len(imgs)} шт...")
        for i, img in enumerate(imgs, 1):
            t_i = time.time()
            f = save_url(img, os.path.join(folder, f"pic{i}"))
            if f:
                files.append(f)
                log(f"      pic{i}: {f} {_fsize(os.path.join(folder, f))} — {fmt_dur(time.time() - t_i)}")
            else:
                log(f"      pic{i}: ОШИБКА — {fmt_dur(time.time() - t_i)}")
        log(f"    [фото] всего {len(files)}/{len(imgs)} — {fmt_dur(time.time() - t)}")
    else:
        t = time.time()
        got = []
        ytdlp_tried = False

        if VIDEO_MODE == 'ytdlp':
            log(f"    [видео] yt-dlp (≤{VIDEO_MAX_HEIGHT}p)...")
            got = _download_ytdlp_capped(video_url, folder)
            ytdlp_tried = True

        if not got and v.get('play'):
            log("    [видео] качаю через tikwm (SD)...")
            f = save_url(v['play'], os.path.join(folder, 'video'))
            if f:
                got = [f]

        if not got and not ytdlp_tried:
            log(f"    [видео] tikwm не вышло, пробуем yt-dlp (≤{VIDEO_MAX_HEIGHT}p)...")
            got = _download_ytdlp_capped(video_url, folder)

        files.extend(got)
        if got:
            log(f"    [видео] {', '.join(got)} {_fsize(os.path.join(folder, got[0]))} "
                f"— {fmt_dur(time.time() - t)}")
        else:
            log(f"    [видео] ОШИБКА — {fmt_dur(time.time() - t)}")

    # --- звук отдельно
    mi = v.get('music_info') or {}
    music_url = mi.get('play') or v.get('music')
    sound_file = None
    if music_url:
        t = time.time()
        log(f"    [звук] {_clip(mi.get('title'), 60)} — {_clip(mi.get('author'), 40)}, качаю...")
        sound_file = save_url(music_url, os.path.join(folder, 'sound'))
        if sound_file:
            log(f"    [звук] {sound_file} {_fsize(os.path.join(folder, sound_file))} — {fmt_dur(time.time() - t)}")
        else:
            log(f"    [звук] ОШИБКА — {fmt_dur(time.time() - t)}")
    else:
        log("    [звук] ссылки на звук нет")

    meta = {
        'id': vid,
        'url': video_url,
        'source': source,
        'description': title,
        'hashtags': re.findall(r'#(\w+)', title),
        'created': _iso(v.get('create_time')),
        'duration': v.get('duration'),
        'stats': {
            'views': v.get('play_count'),
            'likes': v.get('digg_count'),
            'comments': v.get('comment_count'),
            'shares': v.get('share_count'),
            'saves': v.get('collect_count'),
            'downloads': v.get('download_count'),
        },
        'sound': {
            'id': mi.get('id'),
            'title': mi.get('title'),
            'author': mi.get('author'),
            'url': music_url,
            'file': sound_file,
        },
        'files': files,
    }
    if keep_raw:
        meta['raw'] = v

    # --- комментарии (best effort, потоково)
    declared = v.get('comment_count') or 0
    if not with_comments:
        meta['comments_status'] = 'disabled'
    elif not declared:
        meta['comments_status'] = 'none'
        log("    [комменты] у видео их нет")
    elif STATE['comments_dead']:
        meta['comments_status'] = 'unavailable'
        log("    [комменты] пропуск (API недоступен)")
    else:
        log(f"    [комменты] заявлено {declared}, качаю...")
        saved = fetch_comments(vid, folder, declared, keep_raw)
        if saved is None:
            STATE['comments_dead'] = True
            meta['comments_status'] = 'unavailable'
            log("    [комменты] API недоступен, дальше комментарии пропускаю (см. errors.log)")
        else:
            meta['comments_status'] = 'ok'
            meta['comments_file'] = 'comments.jsonl'
            meta['comments_saved'] = saved

    meta['complete'] = bool(files)
    _dump(info_path, meta)
    log(f"    [готово] {vid} — {fmt_dur(time.time() - t_all)}")
    return 'downloaded' if files else 'failed'


# ---------------------------------------------------------------- profile
def download_profile(username: str, with_comments=True, follow=None, max_videos=None,
                     keep_raw=False, with_videos=True, with_likes=False):
    """follow: None | 'followers' | 'following' | 'both'
    Возвращает True, только если профиль выкачан без ошибок по видео."""
    username = username.rstrip('/').split('@')[-1]
    root = username
    STATE['comments_dead'] = False
    con.t0 = time.time()

    try:
        t = time.time()
        log(f"[профиль] @{username}: читаем страницу профиля...")
        info = fetch_profile_page(username)
        if not info:
            log("[-] Не удалось получить профиль (не существует/забанен/блок). Детали в errors.log")
            return False

        os.makedirs(root, exist_ok=True)   # папку создаём только после успешной проверки профиля
        user = info.get('user') or {}
        stats = info.get('stats') or {}
        log(f"    ник: {user.get('nickname')} | видео: {stats.get('videoCount')} | "
            f"подписчики: {stats.get('followerCount')} | лайки: {stats.get('heartCount')} "
            f"— {fmt_dur(time.time() - t)}")
        log(f"    описание: {_clip(user.get('signature'))}")

        # --- аватарка (с дедупом по хэшу)
        t = time.time()
        avatar_url = user.get('avatarLarger') or user.get('avatarMedium') or user.get('avatarThumb')
        avatar_file = None
        if avatar_url:
            log("[аватарка] качаю...")
            avatar_file = save_url(avatar_url, os.path.join(root, f"userpic_{TODAY}"))
            if avatar_file:
                new_path = os.path.join(root, avatar_file)
                size = _fsize(new_path)
                new_hash = _sha(new_path)
                for other in os.listdir(root):
                    if other.startswith('userpic_') and other != avatar_file and not other.endswith('.part'):
                        if _sha(os.path.join(root, other)) == new_hash:
                            os.remove(new_path)
                            avatar_file = f"(без изменений, см. {other})"
                            break
                log(f"    [аватарка] {avatar_file} {size} — {fmt_dur(time.time() - t)}")
            else:
                log(f"    [аватарка] ОШИБКА — {fmt_dur(time.time() - t)}")

        bio_link = user.get('bioLink')
        _dump(os.path.join(root, f"info_{TODAY}.json"), {
            'fetched_at': datetime.datetime.now().isoformat(),
            'username': user.get('uniqueId'),
            'nickname': user.get('nickname'),
            'user_id': user.get('id'),
            'sec_uid': user.get('secUid'),
            'description': user.get('signature'),
            'verified': user.get('verified'),
            'private': user.get('privateAccount'),
            'bio_link': bio_link.get('link') if isinstance(bio_link, dict) else bio_link,
            'created': _iso(user.get('createTime')),
            'region': user.get('region'),
            'language': user.get('language'),
            'stats': {
                'followers': stats.get('followerCount'),
                'following': stats.get('followingCount'),
                'likes_total': stats.get('heartCount'),
                'videos': stats.get('videoCount'),
                'friends': stats.get('friendCount'),
                'likes_given': stats.get('diggCount'),
            },
            'avatar_url': avatar_url,
            'avatar_file': avatar_file,
            'raw': info,   # профиль маленький, raw оставляем всегда
        })
        log(f"[+] info_{TODAY}.json сохранён")

        # --- подписчики / подписки
        if follow and user.get('id'):
            kinds = []
            if follow in ('followers', 'both'):
                kinds.append(('followers', 'followers'))
            if follow in ('following', 'both'):
                kinds.append(('following', 'followings'))
            for kind, key in kinds:
                log(f"[{kind}] качаю потоком...")
                fetch_follow(kind, key, user['id'], root)
        # --- лайки (только если автор их открыл)
        if with_likes:
            if user.get('openFavorite') and user.get('id'):
                declared_likes = (info.get('statsV2') or {}).get('diggCount')
                log("[лайки] профиль открыт, качаю метаданные...")
                fetch_likes(user['id'], root, declared_likes, keep_raw)
            else:
                log("[лайки] автор скрыл, либо он не лайкнул ничего, пропуск")
        if not with_videos:
            log("[видео] пропуск (--no-videos)")
            return True

        # --- список видео
        t = time.time()
        log("[видео] ищем список (yt-dlp)...")
        ids = list_videos_ytdlp(username, user.get('secUid'))
        if not ids and user.get('secUid'):
            log("    [видео] по secUid пусто, пробуем по нику...")
            ids = list_videos_ytdlp(username)
        if not ids:
            log("    [видео] yt-dlp пусто, пробуем tikwm...")
            ids = list_videos_tikwm(username)
        ids = list(dict.fromkeys(ids))
        if max_videos:
            ids = ids[:max_videos]

        declared = stats.get('videoCount')
        log(f"[видео] нашли: {len(ids)} (по странице профиля: {declared}) — {fmt_dur(time.time() - t)}")
        if not ids:
            if declared:
                log("[!] Страница говорит, что видео есть, но получить список не удалось "
                    "(yt-dlp и tikwm не отдали).")
                return False   # видео должны быть, но списка нет -> неудача
            return True        # у профиля реально нет видео

        counters = {'downloaded': 0, 'skipped': 0, 'failed': 0}
        con.start_bar(len(ids))
        for i, vid in enumerate(ids, 1):
            con.set_pos(i)
            log(f"\n[{i}/{len(ids)}] {vid}")
            try:
                counters[process_video(vid, root, username, with_comments, keep_raw)] += 1
            except Exception as e:
                _check_disk(e)   # Errno 28 -> стоп всего процесса
                log_error(f"Профиль {username}, видео {vid}: {e}")
                log(f"    [!] непредвиденная ошибка: {e}")
                counters['failed'] += 1

        con.stop()
        log(f"\nГотово за {fmt_dur(time.time() - con.t0)}: скачано {counters['downloaded']} | "
            f"пропущено {counters['skipped']} | ошибок {counters['failed']}")
        return counters['failed'] == 0
    except OSError as e:
        _check_disk(e)
        raise
    finally:
        con.stop()
