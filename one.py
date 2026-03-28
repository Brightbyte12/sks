import os
import glob
import time
import json
import subprocess
import tempfile
import pygame
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTO_DIR = os.path.join(BASE_DIR, 'photos')
VIDEO_DIR = os.path.join(BASE_DIR, 'videos')
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
PENDING_DELETE_FILE = os.path.join(BASE_DIR, 'pending_delete.json')
PHOTO_EXTS = {'.png', '.jpg', '.jpeg'}
VIDEO_EXTS = {'.mp4', '.mov', '.avi'}
LOG_FILE = os.path.join(BASE_DIR, 'slideshow.log')
FFPLAY_COMMON = [
    'ffplay',
    '-fs',
    '-autoexit',
    '-loglevel',
    'quiet',
    '-an',
    '-fflags',
    'nobuffer',
    '-flags',
    'low_delay',
    '-probesize',
    '32',
    '-analyzeduration',
    '0'
]


def get_interval():
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            return max(1, int(json.load(f).get('photo_interval', 5)))
    except Exception:
        return 5


def _log(msg):
    try:
        line = f"{datetime.now().isoformat(timespec='seconds')} {msg}\n"
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass


def _norm_path(path):
    return os.path.normcase(os.path.abspath(path))


def _load_pending():
    try:
        with open(PENDING_DELETE_FILE, encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, list):
                return set(data)
    except Exception:
        pass
    return set()


def _save_pending(pending):
    try:
        temp_file = tempfile.NamedTemporaryFile(
            'w',
            encoding='utf-8',
            delete=False,
            dir=BASE_DIR,
            suffix='.pending.tmp'
        )
        temp_path = temp_file.name
        temp_file.close()
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(sorted(pending), f)
        os.replace(temp_path, PENDING_DELETE_FILE)
    except Exception:
        pass


def _is_pending(path):
    return _norm_path(path) in _load_pending()


def _try_clear_pending(path):
    pending = _load_pending()
    npath = _norm_path(path)
    if npath not in pending:
        return
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            return
    pending.discard(npath)
    _save_pending(pending)


def _list_media():
    photos = glob.glob(os.path.join(PHOTO_DIR, '*.*'))
    videos = glob.glob(os.path.join(VIDEO_DIR, '*.*'))
    media = []
    for fpath in photos + videos:
        ext = os.path.splitext(fpath)[1].lower()
        if ext in PHOTO_EXTS or ext in VIDEO_EXTS:
            media.append(os.path.abspath(fpath))
    return sorted(media, key=lambda p: os.path.basename(p).lower())


def _ensure_screen():
    if not pygame.get_init():
        pygame.init()
    if not pygame.display.get_init():
        pygame.display.init()
    pygame.mouse.set_visible(False)
    info = pygame.display.Info()
    screen = pygame.display.set_mode((info.current_w, info.current_h), pygame.FULLSCREEN)
    return screen, (info.current_w, info.current_h)


def _ensure_screen_safe():
    try:
        return _ensure_screen()
    except Exception as err:
        _log(f"screen init failed, using ffplay-only mode: {err}")
        return None, None


def _scale_image(img, screen_size):
    img_rect = img.get_rect()
    scale = min(screen_size[0] / img_rect.width, screen_size[1] / img_rect.height)
    new_size = (int(img_rect.width * scale), int(img_rect.height * scale))
    scaled = pygame.transform.smoothscale(img, new_size)
    surface = pygame.Surface(screen_size)
    surface.fill((0, 0, 0))
    pos = ((screen_size[0] - new_size[0]) // 2, (screen_size[1] - new_size[1]) // 2)
    surface.blit(scaled, pos)
    return surface


def _play_image(path, screen, screen_size):
    if screen is None or screen_size is None:
        return _play_image_fallback(path)

    if not pygame.display.get_init():
        screen, screen_size = _ensure_screen_safe()
        if screen is None or screen_size is None:
            return _play_image_fallback(path)

    try:
        img = pygame.image.load(path).convert()
        frame = _scale_image(img, screen_size)
        screen.blit(frame, (0, 0))
        pygame.display.flip()
    except Exception as err:
        _log(f"pygame image render failed for {path}: {err}")
        return _play_image_fallback(path)

    start = time.monotonic()
    while time.monotonic() - start < get_interval():
        for event in pygame.event.get():
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                return None
        if _is_pending(path):
            _try_clear_pending(path)
            break
        if not os.path.exists(path):
            break
        time.sleep(0.1)
    return True


def _play_image_fallback(path):
    proc = None
    try:
        proc = subprocess.Popen(
            FFPLAY_COMMON + ['-f', 'image2', '-loop', '1', path],
            shell=False
        )
        start = time.monotonic()
        while time.monotonic() - start < get_interval():
            if _is_pending(path) or not os.path.exists(path):
                proc.terminate()
                break
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        return True
    except Exception as err:
        _log(f"ffplay image fallback failed for {path}: {err}")
        return False
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=1)
            except Exception:
                proc.kill()


def _play_video(path):
    proc = None
    try:
        # Keep the last rendered photo visible until ffplay process starts.
        proc = subprocess.Popen(
            FFPLAY_COMMON + [path],
            shell=False
        )
        if pygame.display.get_init():
            pygame.display.quit()
        while proc.poll() is None:
            if _is_pending(path) or not os.path.exists(path):
                proc.terminate()
                break
            time.sleep(0.1)
    except Exception as err:
        _log(f"video playback failed for {path}: {err}")
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=1)
            except Exception:
                proc.kill()
        if _is_pending(path):
            _try_clear_pending(path)


def run_slideshow():
    screen, screen_size = _ensure_screen_safe()
    last_path = None

    while True:
        media = [p for p in _list_media() if not _is_pending(p)]
        if not media:
            if screen is not None and pygame.display.get_init():
                screen.fill((20, 20, 20))
                pygame.display.flip()
                pygame.event.pump()
            time.sleep(1)
            continue

        next_index = (media.index(last_path) + 1) % len(media) if last_path in media else 0
        path = media[next_index]
        last_path = path

        if _is_pending(path):
            _try_clear_pending(path)
            continue
        if not os.path.exists(path):
            continue

        ext = os.path.splitext(path)[1].lower()
        if ext in PHOTO_EXTS:
            result = _play_image(path, screen, screen_size)
            if result is None:
                return
            if result is False:
                _log(f"image skipped after both render methods failed: {path}")
            continue

        if ext in VIDEO_EXTS:
            _play_video(path)
            # Re-init pygame lazily only when next photo needs it.
            screen, screen_size = None, None


if __name__ == '__main__':
    run_slideshow()


