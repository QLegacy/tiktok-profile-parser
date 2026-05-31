import sys
import os
import json
import time
import random
import argparse
import itertools
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm
from playwright.sync_api import sync_playwright

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
]

def log_info(msg):
    print(f"\033[94m[*] {msg}\033[0m")

def log_warn(msg):
    print(f"\033[93m[!] {msg}\033[0m")

def log_err(msg):
    print(f"\033[91m[-] {msg}\033[0m")

def log_debug(msg):
    print(f"\033[90m[DEBUG] {msg}\033[0m")

def find_key_recursive(data, target_key):
    if isinstance(data, dict):
        if target_key in data:
            return data[target_key]
        for key, value in data.items():
            result = find_key_recursive(value, target_key)
            if result is not None:
                return result
    elif isinstance(data, list):
        for item in data:
            result = find_key_recursive(item, target_key)
            if result is not None:
                return result
    return None

def load_cookies_into_context(context, filepath):
    if not os.path.exists(filepath):
        log_warn(f"Файл куки {filepath} не найден. Работаем в гостевом режиме.")
        return False
    try:
        log_info(f"Анализируем файл куки: {filepath}...")
        with open(filepath, 'r', encoding='utf-8') as f:
            cookies_data = json.load(f)
        
        raw_cookies = []
        if isinstance(cookies_data, list):
            raw_cookies = cookies_data
        elif isinstance(cookies_data, dict) and "cookies" in cookies_data:
            raw_cookies = cookies_data["cookies"]

        log_debug(f"Найдено кук в файле: {len(raw_cookies)}")
        
        cookie_names = [c.get("name") for c in raw_cookies if c.get("name")]
        log_debug(f"Список кук для импорта: {', '.join(cookie_names[:10])}...")
        
        has_session = any(name in ["sessionid", "sessionid_ss"] for name in cookie_names)
        if not has_session:
            log_warn("Внимание: В файле cookies.json отсутствует кука 'sessionid'. "
                     "Вы будете запущены как НЕАВТОРИЗОВАННЫЙ гость.")
        else:
            log_info("Кука авторизации 'sessionid' обнаружена. Сессия должна быть активной.")

        formatted_cookies = []
        for c in raw_cookies:
            same_site = c.get("sameSite", "Lax")
            if same_site not in ["Strict", "Lax", "None"]:
                same_site = "Lax"
            formatted_cookies.append({
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".tiktok.com"),
                "path": c.get("path", "/"),
                "expires": c.get("expirationDate") or c.get("expires", -1),
                "httpOnly": c.get("httpOnly", False),
                "secure": c.get("secure", False),
                "sameSite": same_site
            })
        
        context.add_cookies(formatted_cookies)
        log_info("Все куки успешно импортированы в контекст браузера.")
        return True
    except Exception as e:
        log_err(f"Ошибка загрузки/анализа куки-файла: {e}")
        return False

def free_mode_generator():
    chars = "abcdefghijklmnopqrstuvwxyz0123456789._"
    for length in range(2, 21):
        for combo in itertools.product(chars, repeat=length):
            yield "".join(combo)

def download_file(url, filepath, headers=None, cookies=None):
    if not url:
        log_warn("Попытка скачать пустой URL.")
        return False
    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        log_debug(f"Файл уже существует и не пуст, пропускаем: {filepath}")
        return True

    log_info(f"Начинаем скачивание медиафайла: {filepath}")
    log_debug(f"URL источника: {url[:100]}...")

    for attempt in range(3):
        try:
            response = requests.get(url, headers=headers, cookies=cookies, stream=True, timeout=30)
            log_debug(f"Ответ сервера при скачивании (Попытка {attempt+1}): {response.status_code}")
            if response.status_code == 200:
                total_size = int(response.headers.get('content-length', 0))
                with open(filepath, 'wb') as f, tqdm(
                    desc=os.path.basename(filepath)[:25],
                    total=total_size,
                    unit='B',
                    unit_scale=True,
                    unit_divisor=1024,
                    leave=False
                ) as bar:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            bar.update(len(chunk))
                
                if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    log_info(f"Файл успешно сохранен: {filepath} ({os.path.getsize(filepath)} B)")
                    return True
            time.sleep(2)
        except Exception as e:
            log_warn(f"Попытка {attempt+1} скачивания не удалась из-за ошибки: {e}")
            time.sleep(2)
            
    if os.path.exists(filepath) and os.path.getsize(filepath) == 0:
        try:
            os.remove(filepath)
        except OSError:
            pass
    log_err(f"Не удалось скачать файл после 3 попыток: {filepath}")
    return False

def parse_comments_recursive(comments_list):
    parsed = []
    if not comments_list:
        return parsed
    
    for c in comments_list:
        text = c.get("text") or c.get("share_desc") or ""
        cid = c.get("cid") or c.get("id") or ""
        create_time = c.get("create_time") or 0
        try:
            date_str = datetime.fromtimestamp(int(create_time)).strftime("%Y-%m-%d %H:%M:%S")
        except:
            date_str = "Unknown"
        
        user_info = c.get("user") or {}
        nickname = user_info.get("nickname") or ""
        unique_id = user_info.get("unique_id") or ""
        avatar = user_info.get("avatar_thumb") or user_info.get("avatar_larger") or ""
        
        replies_raw = c.get("reply_comment") or []
        
        comment_struct = {
            "comment_id": cid,
            "text": text,
            "date": date_str,
            "timestamp": create_time,
            "author": {
                "nickname": nickname,
                "profile_url": f"https://www.tiktok.com/@{unique_id}" if unique_id else "",
                "avatar": avatar
            },
            "likes": c.get("digg_count") or 0,
            "replies": parse_comments_recursive(replies_raw)
        }
        parsed.append(comment_struct)
    return parsed
def human_scroll(page):
    log_debug("Выполняем плавный человеческий скроллинг страницы...")
    current_scroll_position = page.evaluate("window.pageYOffset")
    target_scroll_position = page.evaluate("document.body.scrollHeight")
    
    while current_scroll_position < target_scroll_position:
        step = random.randint(250, 450)
        current_scroll_position += step
        
        page.evaluate(f"window.scrollTo(0, {current_scroll_position})")
        
        time.sleep(random.uniform(0.15, 0.35))
        
        target_scroll_position = page.evaluate("document.body.scrollHeight")
def scroll_profile_and_get_links(page, limit=None):
    log_info("Сканируем список публикаций профиля...")
    
    try:
        page.wait_for_selector("a[href*='/video/'], a[href*='/photo/']", timeout=7000)
        log_info("Элементы публикаций найдены на странице.")
    except Exception:
        log_warn("Публикации на странице не обнаружены. Возможно, мешает капча или блок.")
        input("\033[91m[!] РЕШИТЕ КАПЧУ ИЛИ ВОЙДИТЕ В АККАУНТ В ОКНЕ БРАУЗЕРА, после чего нажмите ENTER в этой консоли...\033[0m")

    video_links = set()
    last_height = page.evaluate("document.body.scrollHeight")
    log_debug(f"Начальная высота прокрутки (scrollHeight): {last_height}")
    
    scroll_attempts = 0
    while True:
        hrefs = page.locator("a[href*='/video/'], a[href*='/photo/']").evaluate_all("elements => elements.map(e => e.href)")
        log_debug(f"Сканирование DOM: обнаружено {len(hrefs)} ссылок.")
        for href in hrefs:
            clean_url = href.split("?")[0]
            video_links.add(clean_url)
            
        if limit and len(video_links) >= limit:
            log_debug(f"Достигнут лимит в {limit} видео. Останавливаем прокрутку.")
            break
            
        scroll_attempts += 1
        log_debug(f"Итерация прокрутки #{scroll_attempts}")
        
        human_scroll(page)
        
        new_height = page.evaluate("document.body.scrollHeight")
        log_debug(f"Новая высота прокрутки после скролла: {new_height}")
        if new_height == last_height:
            log_debug("Высота не изменилась, пробуем подождать еще 3 секунды...")
            page.wait_for_timeout(3000)
            if page.evaluate("document.body.scrollHeight") == last_height:
                log_debug("Высота страницы окончательно стабилизировалась. Завершаем скролл.")
                break
        last_height = new_height        
    result_links = list(video_links)
    if limit:
        result_links = result_links[:limit]
    log_info(f"Итого собрано уникальных публикаций для обработки: {len(result_links)}")
    return result_links
def process_user(username, context, page, limit, save_html):
    username = username.replace("@", "").strip()
    log_info(f"=== НАЧАЛО ОБРАБОТКИ ПРОФИЛЯ: @{username} ===")
    
    user_dir = os.path.join(os.getcwd(), username)
    os.makedirs(user_dir, exist_ok=True)
    log_debug(f"Рабочая директория пользователя создана: {user_dir}")
    
    profile_url = f"https://www.tiktok.com/@{username}"
    log_info(f"Переходим на страницу профиля: {profile_url}")
    try:
        page.goto(profile_url, referer="https://www.google.com")
        log_debug("Ждем загрузки страницы (рандомная пауза)...")
        page.wait_for_timeout(random.randint(2000, 4000))
    except Exception as e:
        log_err(f"Не удалось загрузить страницу @{username}: {e}")
        return
    
    log_debug("Извлекаем HTML-код и ищем блок '__UNIVERSAL_DATA_FOR_REHYDRATION__'...")
    html_content = page.content()
    soup = BeautifulSoup(html_content, "html.parser")
    script_data = soup.find("script", id="__UNIVERSAL_DATA_FOR_REHYDRATION__")
    
    profile_state = {}
    if script_data:
        try:
            profile_state = json.loads(script_data.string)
            log_debug(f"Блок гидратора найден. Размер структуры: {len(script_data.string)} символов.")
        except Exception as e:
            log_err(f"Ошибка декодирования структуры JSON гидратора: {e}")
    else:
        log_warn("Скрипт __UNIVERSAL_DATA_FOR_REHYDRATION__ отсутствует в DOM.")
            
    user_detail = find_key_recursive(profile_state, "webapp.user-detail") or {}
    user_info = user_detail.get("userInfo", {})
    user_data = user_info.get("user", {})
    stats_data = user_info.get("stats", {})
    
    if not user_data:
        log_warn("Глубокие метаданные профиля пусты, используем резервные заполнители.")
        user_data = {"uniqueId": username, "nickname": username}
    
    avatar_url = user_data.get("avatarLarger") or user_data.get("avatarMedium") or user_data.get("avatarThumb")
    if avatar_url:
        ext = ".mp4" if "gif" not in avatar_url and ".mp4" in avatar_url else ".jpg"
        if "gif" in avatar_url:
            ext = ".gif"
        log_debug(f"Обнаружена ссылка на аватарку: {avatar_url[:100]}...")
        download_file(avatar_url, os.path.join(user_dir, f"userpic{ext}"))
        
    is_private = user_data.get("privateAccount", False)
    likes_open = user_data.get("openFavorite", False)
    
    user_json_path = os.path.join(user_dir, "user.json")
    user_meta = {
        "username": user_data.get("uniqueId", username),
        "id": user_data.get("id"),
        "nickname": user_data.get("nickname"),
        "signature": user_data.get("signature"),
        "verified": user_data.get("verified", False),
        "is_private": is_private,
        "followers_count": stats_data.get("followerCount", 0),
        "following_count": stats_data.get("followingCount", 0),
        "likes_received_count": stats_data.get("heartCount", 0),
        "video_count": stats_data.get("videoCount", 0),
        "social_networks": {
            "instagram": user_data.get("insId"),
            "youtube": user_data.get("youtubeChannelId"),
            "twitter": user_data.get("twitterId")
        },
        "sec_uid": user_data.get("secUid")
    }
    
    with open(user_json_path, 'w', encoding='utf-8') as f:
        json.dump(user_meta, f, ensure_ascii=False, indent=4)
    log_info(f"Профиль сохранен в {user_json_path}")
        
    likes_meta_path = os.path.join(user_dir, "likes.json")
    if likes_open:
        log_info("Лайки пользователя открыты для просмотра.")
        with open(likes_meta_path, 'w', encoding='utf-8') as f:
            json.dump({"status": "open", "data": []}, f, ensure_ascii=False)
    else:
        log_info("Лайки пользователя скрыты настройками приватности.")
        with open(likes_meta_path, 'w', encoding='utf-8') as f:
            json.dump({"status": "user hide like"}, f, ensure_ascii=False)

    repost_path = os.path.join(user_dir, "repost.json")
    with open(repost_path, 'w', encoding='utf-8') as f:
        json.dump({"reposts": []}, f, ensure_ascii=False, indent=4)

    video_links = scroll_profile_and_get_links(page, limit)
    
    playwright_cookies = {}
    for c in context.cookies():
        playwright_cookies[c["name"]] = c["value"]
    
    for idx, video_url in enumerate(video_links):
        if "/video/" in video_url:
            video_id = video_url.split("/video/")[-1].split("?")[0]
        elif "/photo/" in video_url:
            video_id = video_url.split("/photo/")[-1].split("?")[0]
        else:
            video_id = video_url.split("/")[-1].split("?")[0]
            
        log_info(f"[{idx+1}/{len(video_links)}] Обработка публикации: {video_id}")
        
        time.sleep(random.randint(2, 7))
        
        tikwm_data = None
        log_debug(f"Отправляем запрос к TikWM API для публикации {video_id}...")
        try:
            api_res = requests.post("https://www.tikwm.com/api/", data={"url": video_url}, timeout=15)
            if api_res.status_code == 200:
                json_res = api_res.json()
                if json_res.get("code") == 0:
                    tikwm_data = json_res.get("data")
                    log_debug("Данные успешно получены через TikWM API (без водяного знака).")
                else:
                    log_warn(f"TikWM вернул код ошибки: {json_res.get('code')}")
        except Exception as e:
            log_warn(f"TikWM недоступен для видео {video_id}: {e}.")
            
        is_images_post = False
        images_urls = []
        music_url = None
        video_download_url = None
        
        if tikwm_data:
            is_images_post = "images" in tikwm_data and bool(tikwm_data["images"])
            images_urls = tikwm_data.get("images", [])
            music_url = tikwm_data.get("music")
            video_download_url = tikwm_data.get("play")
            
            create_time = tikwm_data.get("create_time", 0)
            date_str = datetime.fromtimestamp(int(create_time)).strftime("%Y-%m-%d %H:%M:%S") if create_time else "Unknown"
            
            metadata = {
                "id": video_id,
                "url": video_url,
                "publish_date": date_str,
                "timestamp": create_time,
                "description": tikwm_data.get("title", ""),
                "statistics": {
                    "likes": tikwm_data.get("digg_count", 0),
                    "reposts": tikwm_data.get("share_count", 0),
                    "views": tikwm_data.get("play_count", 0),
                    "favorites": tikwm_data.get("collect_count", 0),
                    "comments_count": tikwm_data.get("comment_count", 0)
                },
                "comments": parse_comments_recursive(tikwm_data.get("comments", []))
            }
        else:
            log_info(f"Переходим на резервный парсинг страницы публикации: {video_url}")
            try:
                page.goto(video_url)
                page.wait_for_timeout(random.randint(2000, 4000))
                v_soup = BeautifulSoup(page.content(), "html.parser")
                v_script = v_soup.find("script", id="__UNIVERSAL_DATA_FOR_REHYDRATION__")
                
                v_state = {}
                if v_script:
                    v_state = json.loads(v_script.string)
                
                item_detail = find_key_recursive(v_state, "itemInfo") or {}
                item_struct = item_detail.get("itemStruct", {})
                
                video_download_url = item_struct.get("video", {}).get("playAddr")
                is_images_post = "imagePostInfo" in item_struct or "/photo/" in video_url
                
                if is_images_post:
                    images_data = item_struct.get("imagePostInfo", {}).get("images", [])
                    images_urls = [img.get("imageURL", {}).get("urlList", [None])[0] for img in images_data if img]
                
                music_url = item_struct.get("music", {}).get("playUrl")
                create_time = int(item_struct.get("createTime", 0))
                date_str = datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S") if create_time else "Unknown"
                
                metadata = {
                    "id": video_id,
                    "url": video_url,
                    "publish_date": date_str,
                    "timestamp": create_time,
                    "description": item_struct.get("desc", ""),
                    "statistics": {
                        "likes": item_struct.get("stats", {}).get("diggCount", 0),
                        "reposts": item_struct.get("stats", {}).get("shareCount", 0),
                        "views": item_struct.get("stats", {}).get("playCount", 0),
                        "favorites": item_struct.get("stats", {}).get("collectCount", 0),
                        "comments_count": item_struct.get("stats", {}).get("commentCount", 0)
                    },
                    "comments": []
                }
            except Exception as e:
                log_err(f"Резервный парсинг публикации {video_id} завершился ошибкой: {e}")
                continue

        if save_html:
            html_dump_path = os.path.join(user_dir, f"{video_id}.html")
            with open(html_dump_path, "w", encoding="utf-8") as html_file:
                html_file.write(page.content())
            log_debug(f"HTML-слепок страницы сохранен: {html_dump_path}")

        headers = {
            "User-Agent": page.evaluate("navigator.userAgent"),
            "Referer": "https://www.tiktok.com/"
        }

        if is_images_post:
            gallery_dir = os.path.join(user_dir, video_id)
            os.makedirs(gallery_dir, exist_ok=True)
            log_debug(f"Создана директория для слайдшоу: {gallery_dir}")
            
            with open(os.path.join(gallery_dir, f"{video_id}.json"), 'w', encoding='utf-8') as f:
                json.dump(metadata, f, ensure_ascii=False, indent=4)
                
            if music_url:
                download_file(music_url, os.path.join(gallery_dir, "audio.mp3"), headers=headers, cookies=playwright_cookies)
                
            for i, img_url in enumerate(images_urls):
                photo_path = os.path.join(gallery_dir, f"photo{i+1}.jpg")
                download_file(img_url, photo_path, headers=headers, cookies=playwright_cookies)
                
            if tikwm_data and "live_photo" in tikwm_data:
                download_file(tikwm_data.get("live_photo"), os.path.join(gallery_dir, f"photo{i+1}.mp4"), headers=headers, cookies=playwright_cookies)
                
            log_info(f"Слайдшоу {video_id} сохранено.")
            
        else:
            video_path = os.path.join(user_dir, f"{video_id}.mp4")
            download_file(video_download_url, video_path, headers=headers, cookies=playwright_cookies)
            
            cover_static = tikwm_data.get("cover") if tikwm_data else None
            cover_dynamic = tikwm_data.get("dynamic_cover") if tikwm_data else None
            
            if cover_static:
                download_file(cover_static, os.path.join(user_dir, f"{video_id}.jpg"), headers=headers, cookies=playwright_cookies)
            if cover_dynamic:
                download_file(cover_dynamic, os.path.join(user_dir, f"{video_id}.webp"), headers=headers, cookies=playwright_cookies)
                
            with open(os.path.join(user_dir, f"{video_id}.json"), 'w', encoding='utf-8') as f:
                json.dump(metadata, f, ensure_ascii=False, indent=4)
                
            log_info(f"Видео {video_id} сохранено.")	
# Перехватчики сетевых событий браузера
def setup_network_monitoring(page):
    def on_request(request):
        try:
            res_type = request.resource_type
            if res_type in ["xhr", "fetch", "document"]:
                log_debug(f"[СЕТЕВОЙ ЗАПРОС] [{res_type.upper()}] -> {request.method} {request.url[:120]}")
        except:
            pass

    def on_response(response):
        try:
            res_type = response.request.resource_type
            if res_type in ["xhr", "fetch", "document"]:
                log_debug(f"[СЕТЕВОЙ ОТВЕТ] [{res_type.upper()}] <- STATUS {response.status} {response.url[:120]}")
        except:
            pass

    def on_request_failed(request):
        try:
            res_type = request.resource_type
            if res_type in ["xhr", "fetch", "document"]:
                # Защита от несовместимости типов в API Playwright
                failure = request.failure
                err_msg = failure if isinstance(failure, str) else getattr(failure, "error_text", str(failure))
                log_warn(f"[СЕТЕВОЙ СБОЙ] [{res_type.upper()}] X {request.url[:120]} | Ошибка: {err_msg}")
        except:
            pass

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)
    
    # Безопасный перенаправитель консоли браузера в терминал
    def on_console(msg):
        try:
            log_info(f"[БРАУЗЕР КОНСОЛЬ] {msg.type.upper()}: {msg.text}")
        except:
            pass
            
    page.on("console", on_console)

def main():
    parser = argparse.ArgumentParser(description="Aperture Science TikTok Archiver Suite")
    parser.add_argument("--single", help="Скачать один конкретный аккаунт (например @qlegacy)")
    parser.add_argument("--accs", help="Список аккаунтов через запятую")
    parser.add_argument("--limit", type=int, help="Лимит последних публикаций для скачивания")
    parser.add_argument("--freeMode", action="store_true", help="Режим тотального парсинга по алфавитному генератору")
    parser.add_argument("--saveHtml", action="store_true", help="Включает сохранение полной HTML-копии страницы публикации")
    args = parser.parse_args()

    target_users = []
    
    if args.single:
        target_users.append(args.single)
    elif args.accs:
        target_users = [u.strip() for u in args.accs.split(",") if u.strip()]
    elif args.freeMode:
        log_info("Активирован режим свободного парсинга по словарной сетке (с длин от 2 до 20).")
        free_gen = free_mode_generator()
        for _ in range(100):
            target_users.append(next(free_gen))
    else:
        log_err("Не указаны параметры запуска. Используйте --single, --accs или --freeMode. Выход.")
        sys.exit(1)

    target_users = [u.replace("@", "").strip() for u in target_users]
    target_users.sort(key=len)
    log_info(f"Пул аккаунтов к обработке: {target_users}")

    with sync_playwright() as p:
        log_info("Инициализируем движок Chromium...")
        # пофиксил: Запуск официального Google Chrome вместо сырого разработческого Chromium
        browser = p.chromium.launch(
            headless=False,
            channel="chrome",  # Запускает системный Google Chrome!
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process"
            ]
        )
        
        linux_ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        
        log_debug("Настраиваем контекст браузера...")
        context = browser.new_context(
            user_agent=linux_ua,
            viewport={"width": 1280, "height": 720},
            locale="ru-RU,ru,en-US,en",
            timezone_id="Europe/Kyiv"
        )
        
        stealth_script = """
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
        const patchWebGL = (proto) => {
            const getParameter = proto.getParameter;
            proto.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Inc.';
                if (parameter === 37446) return 'Intel(R) Iris(TM) Plus Graphics 640';
                return getParameter.apply(this, arguments);
            };
        };
        patchWebGL(WebGLRenderingContext.prototype);
        if (typeof WebGL2RenderingContext !== 'undefined') {
            patchWebGL(WebGL2RenderingContext.prototype);
        }
        Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
        Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
        Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
        window.chrome = {
            app: {
                isInstalled: false,
                InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
                RunningState: { CAN_RUN: 'can_run', CANNOT_RUN: 'cannot_run', RUNNING: 'running' }
            },
            runtime: {
                OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
                OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
                PlatformArch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
                PlatformNaclArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
                PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
                RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' }
            }
        };
        """
        context.add_init_script(stealth_script)
        
        load_cookies_into_context(context, "cookies.json")
        page = context.new_page()
        
        # Подключаем сетевое логирование
        setup_network_monitoring(page)
        
        try:
            for username in target_users:
                try:
                    process_user(username, context, page, args.limit, args.saveHtml)
                except Exception as e:
                    log_err(f"Критическая ошибка при обработке @{username}: {e}")
                time.sleep(random.randint(5, 10))
        except KeyboardInterrupt:
            log_warn("Процесс архивации принудительно остановлен пользователем.")
        finally:
            # Безопасное закрытие
            try:
                context.close()
                browser.close()
            except:
                pass
            log_info("Сессия завершена.")

if __name__ == "__main__":
    main()	
