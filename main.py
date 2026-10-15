import os
import re
import sys
import json
import time
import socket
import shutil
import sqlite3
import argparse
import requests
import subprocess
from pathlib import Path

# Жесткий таймаут на уровне сетевых сокетов, чтобы скрипт не вис наглухо на мертвых прокси
socket.setdefaulttimeout(12)

# Импортируем движок выкачки профилей из profile_downloader.py
from profile_downloader import download_profile, log

NORMAL_DB_FILE = "normal.db"
STORAGE_DB_FILE = "storage.db"
SYNCED_AUTHORS_FILE = "synced_authors.txt"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

AUTHOR_CACHE = {}

CURRENT_PROXY = None
REQUESTS_PROXIES = None

# Троттлинг TikWM (бесплатный лимит: 1 запрос/сек)
LAST_TIKWM = 0.0
TIKWM_MIN_INTERVAL = 1.3


class TransientError(Exception):
    """Временная ошибка (лимит/сеть) — видео НЕ считаем удалённым."""


# ---------------------------------------------------------------------------
# База данных и определение автора
# ---------------------------------------------------------------------------
def get_db_url_map():
    url_map = {}
    if os.path.exists(NORMAL_DB_FILE):
        try:
            with sqlite3.connect(NORMAL_DB_FILE) as conn:
                for vid, url in conn.execute("SELECT id, url FROM normal_videos WHERE id IS NOT NULL"):
                    if vid and url:
                        url_map[str(vid)] = url
        except Exception as e:
            print(f" [DB normal.db err: {e}]", end="", flush=True)

    if os.path.exists(STORAGE_DB_FILE):
        try:
            with sqlite3.connect(STORAGE_DB_FILE) as conn:
                for vid, url in conn.execute("SELECT id, url FROM storage_map WHERE id IS NOT NULL"):
                    if vid and url and str(vid) not in url_map:
                        url_map[str(vid)] = url
        except Exception as e:
            print(f" [DB storage.db err: {e}]", end="", flush=True)
    return url_map


def extract_username_from_url(url: str):
    if not url:
        return None
    m = re.search(r'@([a-zA-Z0-9_.-]+)', url)
    return m.group(1) if m else None


def resolve_short_url(url: str):
    try:
        r = requests.head(
            url,
            headers=HEADERS,
            proxies=REQUESTS_PROXIES,
            allow_redirects=True,
            timeout=8
        )
        return extract_username_from_url(r.url)
    except Exception as e:
        print(f" [Err short: {type(e).__name__}]", end="", flush=True)
        return None


def tikwm_throttle():
    global LAST_TIKWM
    wait = TIKWM_MIN_INTERVAL - (time.time() - LAST_TIKWM)
    if wait > 0:
        time.sleep(wait)
    LAST_TIKWM = time.time()


def fetch_author_from_oembed(video_id: str):
    """Быстрый способ без жёсткого лимита. Возвращает (author, transient)."""
    try:
        r = requests.get(
            "https://www.tiktok.com/oembed",
            params={"url": f"https://www.tiktok.com/@a/video/{video_id}"},
            headers=HEADERS,
            proxies=REQUESTS_PROXIES,
            timeout=10
        )
        if r.status_code == 200:
            j = r.json()
            return extract_username_from_url(j.get('author_url', '')), False
        if r.status_code in (429, 500, 502, 503, 504):
            return None, True
        return None, False
    except Exception as e:
        print(f" [oEmbed Err: {type(e).__name__}]", end="", flush=True)
        return None, True


def fetch_author_from_embed(video_id: str):
    """Embed-страница. Нестабильна (503), поэтому только как быстрая попытка."""
    try:
        r = requests.get(
            f"https://www.tiktok.com/embed/v2/{video_id}",
            headers=HEADERS, proxies=REQUESTS_PROXIES, timeout=10
        )
        if r.status_code == 200:
            m = re.search(r'"uniqueId"\s*:\s*"([^"]+)"', r.text)
            if m:
                return m.group(1)
    except Exception as e:
        print(f" [embed Err: {type(e).__name__}]", end="", flush=True)
    return None


def fetch_author_from_tikwm(video_id: str, retries: int = 4):
    """Возвращает (author, transient)."""
    fake_url = f"https://www.tiktok.com/@a/video/{video_id}"
    for attempt in range(retries):
        tikwm_throttle()
        try:
            resp = requests.get(
                "https://www.tikwm.com/api/",
                params={"url": fake_url, "hd": 1},
                headers=HEADERS,
                proxies=REQUESTS_PROXIES,
                timeout=10
            )
            if resp.status_code != 200:
                print(f" [TikWM HTTP {resp.status_code}]", end="", flush=True)
                time.sleep(2)
                continue

            j = resp.json()
            if j.get('code') == 0 and 'data' in j:
                author = j['data'].get('author', {}).get('unique_id')
                return (author, False) if author else (None, False)

            msg = str(j.get('msg', ''))
            if 'limit' in msg.lower():
                time.sleep(1.5 + attempt)  # ждём и повторяем
                continue

            print(f" [TikWM API: {msg[:40]}]", end="", flush=True)
            return None, False  # реальный отказ: видео недоступно
        except Exception as e:
            print(f" [TikWM Err: {type(e).__name__}]", end="", flush=True)
            time.sleep(2)
    return None, True  # так и не получили внятного ответа


def fetch_author_from_ytdlp(video_id: str):
    """Возвращает (author, transient)."""
    if shutil.which("yt-dlp") is None:
        print(" [yt-dlp не установлен]", end="", flush=True)
        return None, False
    try:
        cmd = ["yt-dlp", "--dump-json", "--no-warnings", "--socket-timeout", "10"]
        if CURRENT_PROXY:
            cmd.extend(["--proxy", CURRENT_PROXY])
        cmd.append(f"https://www.tiktok.com/@a/video/{video_id}")

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            # ВАЖНО: uploader_id у TikTok — числовой ID, юзернейм в uploader / url
            candidates = [
                extract_username_from_url(data.get('uploader_url', '')),
                data.get('uploader'),
                extract_username_from_url(data.get('webpage_url', '')),
            ]
            for a in candidates:
                if a and a != 'a' and not a.isdigit() and re.fullmatch(r'[\w.\-]+', a):
                    return a, False
            return None, False
        err = res.stderr.strip().split('\n')[-1] if res.stderr else "code != 0"
        print(f" [yt-dlp: {err[:50]}]", end="", flush=True)
        # сетевые сбои/лимиты — временные, остальное считаем «видео недоступно»
        if re.search(r'429|timed out|timeout|Unable to download|Connection|Temporary|Too Many',
                     err, re.I):
            return None, True
        return None, False
    except subprocess.TimeoutExpired:
        print(" [yt-dlp timeout]", end="", flush=True)
        return None, True
    except Exception as e:
        print(f" [yt-dlp Err: {type(e).__name__}]", end="", flush=True)
        return None, True


def determine_author(video_id: str, known_url: str):
    """Возвращает автора, None (видео реально недоступно) или кидает TransientError."""
    if video_id in AUTHOR_CACHE:
        return AUTHOR_CACHE[video_id]

    author = None
    transient = False

    if known_url:
        author = extract_username_from_url(known_url)

    if not author and known_url and any(s in known_url for s in ('vm.tiktok.com', 'vt.tiktok.com', '/t/')):
        print(" [short-url...]", end="", flush=True)
        author = resolve_short_url(known_url)

    # oEmbed (всегда 429) и embed (503 и ~5с) по тестам бесполезны — функции
    # оставлены выше, но в цепочку не включены.
    # Авторитетные источники: только они решают, удалено видео или нет
    definitive = False

    if not author:
        print(" [TikWM...]", end="", flush=True)
        author, t = fetch_author_from_tikwm(video_id)
        if not author and not t:
            definitive = True

    if not author:
        print(" [yt-dlp...]", end="", flush=True)
        author, t = fetch_author_from_ytdlp(video_id)
        if not author and not t:
            definitive = True

    if author:
        author = author.strip('@')
        AUTHOR_CACHE[video_id] = author
        return author

    if definitive:
        return None          # реально недоступно -> _unknown_or_deleted
    raise TransientError()   # лимиты/сеть -> оставляем на месте


# ---------------------------------------------------------------------------
# Адаптация структуры файлов под profile_downloader
# ---------------------------------------------------------------------------
def normalize_video_folder(folder_path: str, vid: str):
    """
    Приводит файлы внутри папки видео к структуре profile_downloader:
    7678.mp4 -> video.mp4
    7678_audio.mp3 -> sound.mp3
    7678_pic1.jpg -> pic1.jpg
    Создает video_info.json, чтобы profile_downloader знал, что видео уже скачано.
    """
    try:
        for fname in os.listdir(folder_path):
            old_file = os.path.join(folder_path, fname)
            if not os.path.isfile(old_file):
                continue

            if fname == f"{vid}.mp4":
                new_file = os.path.join(folder_path, "video.mp4")
                if not os.path.exists(new_file):
                    os.rename(old_file, new_file)
            elif fname == f"{vid}_audio.mp3":
                new_file = os.path.join(folder_path, "sound.mp3")
                if not os.path.exists(new_file):
                    os.rename(old_file, new_file)
            elif fname.startswith(f"{vid}_pic"):
                new_name = fname.replace(f"{vid}_", "")
                new_file = os.path.join(folder_path, new_name)
                if not os.path.exists(new_file):
                    os.rename(old_file, new_file)

        info_path = os.path.join(folder_path, "video_info.json")
        if not os.path.exists(info_path):
            with open(info_path, "w", encoding="utf-8") as f:
                json.dump({
                    "id": vid,
                    "complete": True,
                    "comments_status": "none",
                    "migrated_from_favorites": True
                }, f, indent=2)
    except Exception as e:
        print(f" [!] Ошибка адаптации файлов {vid}: {e}")


def safe_move_and_merge(src_dir: str, dst_dir: str):
    if not os.path.exists(dst_dir):
        shutil.move(src_dir, dst_dir)
    else:
        for item in os.listdir(src_dir):
            s = os.path.join(src_dir, item)
            d = os.path.join(dst_dir, item)
            if not os.path.exists(d):
                shutil.move(s, d)
            elif os.path.isfile(s):
                os.remove(s)
        try:
            os.rmdir(src_dir)
        except OSError:
            pass


def retry_unknown_videos(work_dir: Path, url_map: dict):
    unknown_base = work_dir / "_unknown_or_deleted"
    if not unknown_base.exists() or not unknown_base.is_dir():
        print("[i] Папка _unknown_or_deleted не найдена или пуста.")
        return set()

    candidates = [
        d for d in os.listdir(unknown_base)
        if os.path.isdir(unknown_base / d) and re.match(r'^\d{15,20}$', d)
    ]

    if not candidates:
        print("[i] В _unknown_or_deleted нет роликов для повторной проверки.")
        return set()

    print(f"\n[🔄] Повторная попытка для {len(candidates)} неопознанных видео...")
    recovered_authors = set()
    recovered_count = 0

    for idx, vid in enumerate(candidates, 1):
        src_path = str(unknown_base / vid)
        known_url = url_map.get(vid)

        print(f"[{idx}/{len(candidates)}] Перепроверка {vid}", end="", flush=True)

        try:
            author = determine_author(vid, known_url)
        except TransientError:
            print(" -> [~] временная ошибка, пропускаю")
            continue

        if author:
            recovered_authors.add(author)
            recovered_count += 1
            target_author_dir = str(work_dir / author)
            final_video_dir = os.path.join(target_author_dir, vid)

            os.makedirs(target_author_dir, exist_ok=True)
            safe_move_and_merge(src_path, final_video_dir)
            normalize_video_folder(final_video_dir, vid)
            print(f" -> ВОССТАНОВЛЕНО: @{author}/{vid} [OK]")
        else:
            print(" -> [x] Всё еще недоступно.")

    try:
        if not os.listdir(unknown_base):
            unknown_base.rmdir()
    except OSError:
        pass

    print(f"[✓] Успешно восстановлено: {recovered_count} из {len(candidates)}\n")
    return recovered_authors


def load_synced_authors():
    if os.path.exists(SYNCED_AUTHORS_FILE):
        with open(SYNCED_AUTHORS_FILE, 'r', encoding='utf-8') as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def mark_author_synced(author: str):
    with open(SYNCED_AUTHORS_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{author}\n")


# ---------------------------------------------------------------------------
# Главная логика
# ---------------------------------------------------------------------------
def main():
    global CURRENT_PROXY, REQUESTS_PROXIES

    parser = argparse.ArgumentParser(description="Сортировка избранного по авторам + полная выкачка их профилей")
    parser.add_argument('--dir', default='.', help='Папка с видео (по умолчанию текущая)')
    parser.add_argument('--proxy', type=str, default=None,
                        help='SOCKS5/HTTP прокси (например: socks5h://user:pass@host:port или socks5h://127.0.0.1:1080)')
    parser.add_argument('--no-comments', action='store_true', default=True,
                        help='Не качать комментарии ко всем новым видео (по умолчанию включено)')
    parser.add_argument('--with-comments', dest='no_comments', action='store_false',
                        help='Качать комментарии ко всем видео')
    parser.add_argument('--max-videos-per-author', type=int, default=None,
                        help='Лимит новых видео на автора')
    parser.add_argument('--delay', type=float, default=2.0,
                        help='Пауза между авторами в секундах (по умолчанию 2.0с)')
    parser.add_argument('--skip-sync', action='store_true',
                        help='Только отсортировать и адаптировать папки, НЕ качать профили')
    parser.add_argument('--retry-unknown', action='store_true',
                        help='Повторно проверить видео из папки _unknown_or_deleted')
    parser.add_argument('--likes', action='store_true',
                        help='Сохранять список лайкнутых видео (если автор открыл лайки)')
    args = parser.parse_args()

    # Настройка прокси
    if args.proxy:
        proxy_url = args.proxy
        if not re.match(r'^(socks5h?|https?|socks4)://', proxy_url):
            proxy_url = f"socks5h://{proxy_url}"

        CURRENT_PROXY = proxy_url
        REQUESTS_PROXIES = {
            'http': proxy_url,
            'https': proxy_url
        }

        os.environ['HTTP_PROXY'] = proxy_url
        os.environ['HTTPS_PROXY'] = proxy_url
        os.environ['ALL_PROXY'] = proxy_url
        print(f"[i] Прокси активен: {proxy_url}\n")

    work_dir = Path(args.dir).resolve()
    url_map = get_db_url_map()
    authors_found = set()

    if args.retry_unknown:
        recovered = retry_unknown_videos(work_dir, url_map)
        authors_found.update(recovered)

    print("=" * 60)
    print("ЭТАП 1: Поиск и сортировка уже скачанных папок по авторам")
    print("=" * 60)

    candidates = [
        d for d in os.listdir(work_dir)
        if os.path.isdir(os.path.join(work_dir, d)) and re.match(r'^\d{15,20}$', d)
    ]

    print(f"Найдено локальных папок с видео: {len(candidates)}")

    unknown_count = 0
    transient_count = 0

    for idx, vid in enumerate(candidates, 1):
        src_path = os.path.join(work_dir, vid)
        known_url = url_map.get(vid)

        print(f"[{idx}/{len(candidates)}] Видео {vid}", end="", flush=True)

        try:
            author = determine_author(vid, known_url)
        except TransientError:
            print(" -> [~] временная ошибка сети/лимита, оставляю на месте (повторится при следующем запуске)")
            transient_count += 1
            continue

        if not author:
            print(" -> [!] Автор не найден / видео удалено (переносим в _unknown_or_deleted)")
            unknown_dir = os.path.join(work_dir, "_unknown_or_deleted", vid)
            os.makedirs(os.path.join(work_dir, "_unknown_or_deleted"), exist_ok=True)
            safe_move_and_merge(src_path, unknown_dir)
            unknown_count += 1
            continue

        authors_found.add(author)
        target_author_dir = os.path.join(work_dir, author)
        final_video_dir = os.path.join(target_author_dir, vid)

        os.makedirs(target_author_dir, exist_ok=True)
        safe_move_and_merge(src_path, final_video_dir)
        normalize_video_folder(final_video_dir, vid)

        print(f" -> @{author}/{vid} [OK]")

    for d in os.listdir(work_dir):
        full_p = os.path.join(work_dir, d)
        if os.path.isdir(full_p) and not d.startswith(('_', '.')) and not re.match(r'^\d{15,20}$', d):
            authors_found.add(d)

    print("\n" + "=" * 60)
    print("ЭТАП 1 ЗАВЕРШЕН!")
    print(f"Уникальных авторов обнаружено: {len(authors_found)}")
    print(f"Удаленных/неопознанных видео: {unknown_count}")
    print(f"Пропущено из-за временных ошибок: {transient_count}")
    print("=" * 60 + "\n")

    if args.skip_sync:
        print("[!] Флаг --skip-sync включен. Выкачка профилей пропущена.")
        return

    print("=" * 60)
    print("ЭТАП 2: Полная выкачка профилей (аватарки, инфо, остальные видео)")
    print("=" * 60)

    synced_authors = load_synced_authors()
    authors_queue = sorted(list(authors_found))

    for idx, author in enumerate(authors_queue, 1):
        if author in synced_authors:
            print(f"[{idx}/{len(authors_queue)}] @{author} -> уже выкачан ранее (пропуск)")
            continue

        print(f"\n[{idx}/{len(authors_queue)}] >>> Начинаем полную выкачку автора: @{author}")

        try:
            ok = download_profile(
                username=author,
                with_comments=not args.no_comments,
                max_videos=args.max_videos_per_author,
                with_videos=True,
                with_likes=args.likes
            )
            if ok:
                mark_author_synced(author)
                print(f"[+] Автор @{author} успешно обработан и сохранен.")
            else:
                print(f"[!] Автор @{author} НЕ выкачан (см. errors.log), "
                      f"в synced не записываю — повторится при следующем запуске.")
        except Exception as e:
            print(f"[!] Ошибка при выкачке автора @{author}: {e}")

        time.sleep(args.delay)

    print("\n" + "=" * 60)
    print("ВСЁ ПОЛНОСТЬЮ ГОТОВО! Все авторы и видео синхронизированы.")
    print("=" * 60)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Прервано пользователем.")
        sys.exit(130)
