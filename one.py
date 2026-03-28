from flask import Flask, request, redirect, url_for, render_template_string
import os, json, subprocess, sys, tempfile, signal, time, socket
from werkzeug.utils import secure_filename

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTO_FOLDER = os.path.join(BASE_DIR, 'photos')
VIDEO_FOLDER = os.path.join(BASE_DIR, 'videos')
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
PENDING_DELETE_FILE = os.path.join(BASE_DIR, 'pending_delete.json')
SLIDESHOW_PID_FILE = os.path.join(BASE_DIR, 'slideshow.pid')
SLIDESHOW_OUT_LOG_FILE = os.path.join(BASE_DIR, 'slideshow.out.log')
SLIDESHOW_ERR_LOG_FILE = os.path.join(BASE_DIR, 'slideshow.err.log')
ALLOWED_PHOTO_EXTS = {'.png', '.jpg', '.jpeg'}
ALLOWED_VIDEO_EXTS = {'.mp4', '.mov', '.avi'}

slideshow_process = None

for folder in [PHOTO_FOLDER, VIDEO_FOLDER]:
    os.makedirs(folder, exist_ok=True)


def get_lan_ip():
    """Best-effort local LAN IP for showing phone-access URL."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def get_config():
    default = {'photo_interval': 5}
    if not os.path.exists(CONFIG_FILE):
        return default
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            data = json.load(f)
        interval = int(data.get('photo_interval', default['photo_interval']))
        return {'photo_interval': max(1, interval)}
    except Exception:
        return default


def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f)


def is_running():
    global slideshow_process
    if slideshow_process is not None and slideshow_process.poll() is None:
        return True
    pid = _read_pid()
    running = pid is not None and _pid_exists(pid)
    if pid is not None and not running:
        _clear_pid()
    return running


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
    temp_file = tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False, dir=BASE_DIR, suffix='.pending.tmp')
    temp_path = temp_file.name
    temp_file.close()
    with open(temp_path, 'w', encoding='utf-8') as f:
        json.dump(sorted(pending), f)
    os.replace(temp_path, PENDING_DELETE_FILE)


def _add_pending(path):
    pending = _load_pending()
    pending.add(_norm_path(path))
    _save_pending(pending)


def _remove_pending(path):
    pending = _load_pending()
    pending.discard(_norm_path(path))
    _save_pending(pending)


def _is_within(base_folder, path):
    base = _norm_path(base_folder)
    target = _norm_path(path)
    return os.path.commonpath([base, target]) == base


def _resolve_media_path(media_type, filename):
    folder = PHOTO_FOLDER if media_type == 'photo' else VIDEO_FOLDER
    safe_name = secure_filename(filename)
    full_path = os.path.abspath(os.path.join(folder, safe_name))
    if not _is_within(folder, full_path):
        return None
    return full_path


def _write_pid(pid):
    temp_file = tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False, dir=BASE_DIR, suffix='.pid.tmp')
    temp_path = temp_file.name
    temp_file.close()
    with open(temp_path, 'w', encoding='utf-8') as f:
        f.write(str(pid))
    os.replace(temp_path, SLIDESHOW_PID_FILE)


def _read_pid():
    if not os.path.exists(SLIDESHOW_PID_FILE):
        return None
    try:
        with open(SLIDESHOW_PID_FILE, encoding='utf-8') as f:
            return int(f.read().strip())
    except Exception:
        return None


def _clear_pid():
    try:
        if os.path.exists(SLIDESHOW_PID_FILE):
            os.remove(SLIDESHOW_PID_FILE)
    except Exception:
        pass


def _pid_exists(pid):
    if pid <= 0:
        return False
    try:
        if os.name == 'nt':
            result = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}'],
                capture_output=True,
                text=True
            )
            return str(pid) in result.stdout
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _kill_pid(pid):
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
        else:
            # Prefer killing the whole process group on POSIX so ffplay children are cleaned up too.
            try:
                os.killpg(pid, signal.SIGTERM)
                time.sleep(0.4)
                if _pid_exists(pid):
                    os.killpg(pid, signal.SIGKILL)
            except Exception:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.2)
                if _pid_exists(pid):
                    os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def _slideshow_env():
    env = os.environ.copy()
    if os.name != 'nt' and not env.get('DISPLAY'):
        # Typical Raspberry Pi desktop display.
        env['DISPLAY'] = ':0'
    return env


HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Media Control</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root {
            --ink: #1f2a37;
            --muted: #5f6b7a;
            --line: #dde4ee;
            --paper: #ffffff;
            --accent: #ea580c;
            --accent-deep: #c2410c;
            --blue: #0f4c81;
            --blue-soft: #e4f1ff;
            --good-soft: #d9fbe7;
            --good: #166534;
            --bad-soft: #ffe4e6;
            --bad: #9f1239;
            --danger: #dc2626;
            --surface: #fffaf3;
            --shadow: 0 18px 34px rgba(15, 23, 42, 0.12);
            --radius-lg: 20px;
            --radius-md: 14px;
            --radius-sm: 11px;
        }

        * { box-sizing: border-box; }

        body {
            margin: 0;
            min-height: 100vh;
            padding: 24px 14px 36px;
            font-family: "Segoe UI Variable", "Trebuchet MS", "Gill Sans", sans-serif;
            color: var(--ink);
            background:
                radial-gradient(circle at 10% 15%, #ffe1c7 0%, transparent 34%),
                radial-gradient(circle at 88% 5%, #d6ecff 0%, transparent 36%),
                linear-gradient(165deg, #fffdf9 0%, #f6f8fb 100%);
            display: flex;
            justify-content: center;
        }

        .shell {
            width: 100%;
            max-width: 1040px;
            display: grid;
            gap: 16px;
            animation: rise 320ms ease;
        }

        @keyframes rise {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .hero {
            background: linear-gradient(130deg, #0f4c81 0%, #0b355a 100%);
            color: #f8fbff;
            border-radius: var(--radius-lg);
            padding: 20px;
            box-shadow: 0 20px 44px rgba(15, 76, 129, 0.35);
            position: relative;
            overflow: hidden;
        }

        .hero::after {
            content: "";
            position: absolute;
            width: 240px;
            height: 240px;
            border-radius: 999px;
            background: radial-gradient(circle, rgba(255, 255, 255, 0.24), rgba(255, 255, 255, 0));
            top: -90px;
            right: -70px;
        }

        .hero h1 {
            margin: 0;
            font-size: 29px;
            letter-spacing: 0.2px;
            position: relative;
            z-index: 1;
        }

        .hero p {
            margin: 6px 0 0;
            font-size: 14px;
            opacity: 0.9;
            position: relative;
            z-index: 1;
        }

        .status {
            margin-top: 14px;
            display: inline-flex;
            border-radius: 999px;
            padding: 8px 13px;
            font-size: 12px;
            font-weight: 800;
            letter-spacing: 0.4px;
            position: relative;
            z-index: 1;
        }

        .status.on {
            background: var(--good-soft);
            color: var(--good);
        }

        .status.off {
            background: var(--bad-soft);
            color: var(--bad);
        }

        .quick-stats {
            margin-top: 10px;
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            position: relative;
            z-index: 1;
        }

        .quick-stats span {
            border-radius: 999px;
            border: 1px solid rgba(255, 255, 255, 0.28);
            background: rgba(255, 255, 255, 0.14);
            padding: 6px 10px;
            font-size: 12px;
            font-weight: 700;
        }

        .controls {
            margin-top: 14px;
            display: grid;
            grid-template-columns: 1fr;
            gap: 10px;
            position: relative;
            z-index: 1;
        }

        .grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: 16px;
        }

        .card {
            background: var(--paper);
            border: 1px solid var(--line);
            border-radius: var(--radius-lg);
            box-shadow: var(--shadow);
            padding: 18px;
        }

        .card h3 {
            margin: 0 0 8px;
            font-size: 19px;
        }

        .help {
            margin: 0 0 12px;
            color: var(--muted);
            font-size: 13px;
        }

        label {
            display: block;
            margin-bottom: 7px;
            color: var(--muted);
            font-size: 13px;
            font-weight: 700;
        }

        input[type="number"],
        input[type="file"] {
            width: 100%;
            border: 1px solid #ced8e6;
            border-radius: var(--radius-sm);
            background: #f9fbff;
            color: var(--ink);
            padding: 11px 12px;
            font-size: 15px;
            outline: none;
            transition: border-color 0.16s ease, box-shadow 0.16s ease;
        }

        input:focus {
            border-color: var(--accent);
            box-shadow: 0 0 0 3px rgba(234, 88, 12, 0.18);
        }

        button {
            width: 100%;
            border: 0;
            border-radius: var(--radius-sm);
            font-size: 14px;
            font-weight: 800;
            letter-spacing: 0.3px;
            padding: 12px 14px;
            cursor: pointer;
            transition: transform 0.14s ease, filter 0.14s ease;
        }

        button:hover { transform: translateY(-1px); filter: brightness(0.98); }
        button:active { transform: translateY(0); }

        .btn-start {
            background: #22c55e;
            color: #052e16;
        }

        .btn-stop {
            background: #ef4444;
            color: #fff;
        }

        .btn-main {
            margin-top: 10px;
            background: linear-gradient(120deg, var(--accent) 0%, var(--accent-deep) 100%);
            color: #fff9f6;
        }

        .library-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
            margin-bottom: 10px;
        }

        .count {
            font-size: 12px;
            font-weight: 800;
            color: var(--blue);
            background: var(--blue-soft);
            border-radius: 999px;
            padding: 5px 10px;
            border: 1px solid #cbe3fb;
        }

        .list {
            display: grid;
            gap: 8px;
        }

        .item {
            background: var(--surface);
            border: 1px solid #ecdfd2;
            border-radius: var(--radius-md);
            padding: 9px 10px;
            display: grid;
            grid-template-columns: auto 1fr auto;
            gap: 10px;
            align-items: center;
        }

        .pill {
            font-size: 11px;
            font-weight: 800;
            border-radius: 999px;
            padding: 4px 8px;
            color: #0c4a6e;
            background: #dff4ff;
            border: 1px solid #bfe7ff;
        }

        .pill.video {
            color: #7c2d12;
            background: #ffe8d6;
            border: 1px solid #ffcfac;
        }

        .filename {
            min-width: 0;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            font-size: 13px;
            font-weight: 700;
        }

        .delete {
            width: auto;
            padding: 7px 10px;
            font-size: 12px;
            border-radius: 9px;
            background: #fff1f2;
            color: var(--danger);
            border: 1px solid #fecdd3;
        }

        .empty {
            border: 1px dashed #d8dee8;
            border-radius: var(--radius-md);
            padding: 14px;
            color: var(--muted);
            text-align: center;
            background: #fbfcff;
            font-size: 13px;
        }

        @media (min-width: 900px) {
            .controls { grid-template-columns: 1fr 1fr; }
            .grid { grid-template-columns: 1fr 1fr; }
        }
    </style>
</head>
<body>
    <div class="shell">
        <section class="hero">
            <h1>Display Command Center</h1>
            <p>Control your slideshow, uploads, and timing from one screen.</p>
            <div class="status {{ 'on' if running else 'off' }}">{{ 'RUNNING' if running else 'STOPPED' }}</div>
            <div class="quick-stats">
                <span>Images: {{ photos|length }}</span>
                <span>Videos: {{ videos|length }}</span>
                <span>Photo Duration: {{ config.photo_interval }}s</span>
            </div>
            <div class="controls">
                <form method="post" action="/start"><button class="btn-start">START</button></form>
                <form method="post" action="/stop"><button class="btn-stop">STOP</button></form>
            </div>
        </section>

        <div class="grid">
            <section class="card">
                <h3>Playback Settings</h3>
                <p class="help">Image slides will stay on screen for the selected duration.</p>
                <form method="post" action="/set_interval">
                    <label>Photo Duration (seconds)</label>
                    <input type="number" name="interval" value="{{ config.photo_interval }}" min="1">
                    <button class="btn-main">SAVE DURATION</button>
                </form>
            </section>

            <section class="card">
                <h3>Upload Media</h3>
                <p class="help">Supported formats: JPG, JPEG, PNG, MP4, MOV, AVI.</p>
                <form method="post" enctype="multipart/form-data" action="/upload">
                    <label>Choose File</label>
                    <input type="file" name="file" accept="image/*,video/*">
                    <button class="btn-main">UPLOAD FILE</button>
                </form>
            </section>
        </div>

        <section class="card">
            <div class="library-header">
                <h3 style="margin: 0;">Media Library</h3>
                <span class="count">Total: {{ photos|length + videos|length }}</span>
            </div>

            {% if not photos and not videos %}
                <div class="empty">No media uploaded yet. Add images or videos to start the slideshow.</div>
            {% endif %}

            {% if photos %}
                <div class="list" style="margin-bottom: 10px;">
                    {% for p in photos %}
                    <div class="item">
                        <span class="pill">PHOTO</span>
                        <span class="filename">{{ p }}</span>
                        <form method="post" action="{{ url_for('delete_file', media_type='photo', filename=p) }}">
                            <button class="delete">DELETE</button>
                        </form>
                    </div>
                    {% endfor %}
                </div>
            {% endif %}

            {% if videos %}
                <div class="list">
                    {% for v in videos %}
                    <div class="item">
                        <span class="pill video">VIDEO</span>
                        <span class="filename">{{ v }}</span>
                        <form method="post" action="{{ url_for('delete_file', media_type='video', filename=v) }}">
                            <button class="delete">DELETE</button>
                        </form>
                    </div>
                    {% endfor %}
                </div>
            {% endif %}
        </section>
    </div>
</body>
</html>
"""


@app.route('/')
def index():
    photos = [f for f in os.listdir(PHOTO_FOLDER) if os.path.splitext(f)[1].lower() in ALLOWED_PHOTO_EXTS]
    videos = [f for f in os.listdir(VIDEO_FOLDER) if os.path.splitext(f)[1].lower() in ALLOWED_VIDEO_EXTS]
    return render_template_string(
        HTML_TEMPLATE,
        config=get_config(),
        running=is_running(),
        photos=sorted(photos),
        videos=sorted(videos)
    )


@app.route('/start', methods=['POST'])
def start_slideshow():
    global slideshow_process
    pid = _read_pid()
    if pid is not None and _pid_exists(pid):
        return redirect(url_for('index'))

    if slideshow_process is None or slideshow_process.poll() is not None:
        script_path = os.path.join(BASE_DIR, 'slideshow.py')
        kwargs = {'cwd': BASE_DIR, 'env': _slideshow_env()}
        if os.name != 'nt':
            # Run slideshow as its own session so stop can terminate descendants reliably.
            kwargs['start_new_session'] = True
        with open(SLIDESHOW_OUT_LOG_FILE, 'a', encoding='utf-8') as out_log, open(
            SLIDESHOW_ERR_LOG_FILE, 'a', encoding='utf-8'
        ) as err_log:
            slideshow_process = subprocess.Popen(
                [sys.executable, script_path],
                stdout=out_log,
                stderr=err_log,
                **kwargs
            )
        _write_pid(slideshow_process.pid)
        # If process exits immediately, clear stale pid.
        time.sleep(0.2)
        if slideshow_process.poll() is not None:
            _clear_pid()
    return redirect(url_for('index'))


@app.route('/stop', methods=['POST'])
def stop_slideshow():
    global slideshow_process
    if slideshow_process and slideshow_process.poll() is None:
        _kill_pid(slideshow_process.pid)
        slideshow_process = None

    pid = _read_pid()
    if pid is not None:
        _kill_pid(pid)
    _clear_pid()
    return redirect(url_for('index'))


@app.route('/upload', methods=['POST'])
def upload_file():
    file = request.files.get('file')
    if file and file.filename:
        filename = secure_filename(file.filename)
        ext = os.path.splitext(filename)[1].lower()
        if ext in ALLOWED_PHOTO_EXTS:
            folder = PHOTO_FOLDER
        elif ext in ALLOWED_VIDEO_EXTS:
            folder = VIDEO_FOLDER
        else:
            return redirect(url_for('index'))
        saved_path = os.path.join(folder, filename)
        file.save(saved_path)
        _remove_pending(saved_path)
    return redirect(url_for('index'))


@app.route('/set_interval', methods=['POST'])
def set_interval():
    config = get_config()
    try:
        interval = int(request.form.get('interval', config.get('photo_interval', 5)))
    except (TypeError, ValueError):
        interval = config.get('photo_interval', 5)
    config['photo_interval'] = max(1, interval)
    save_config(config)
    return redirect(url_for('index'))


@app.route('/delete/<media_type>/<filename>', methods=['POST'])
def delete_file(media_type, filename):
    if media_type not in {'photo', 'video'}:
        return redirect(url_for('index'))

    path = _resolve_media_path(media_type, filename)
    if path is None:
        return redirect(url_for('index'))

    if os.path.exists(path):
        try:
            os.remove(path)
            _remove_pending(path)
        except Exception:
            _add_pending(path)
    else:
        _remove_pending(path)
    return redirect(url_for('index'))


if __name__ == '__main__':
    host = os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', '5000'))
    if host == '0.0.0.0':
        print(f"Open from phone on same Wi-Fi: http://{get_lan_ip()}:{port}")
    app.run(host=host, port=port)












----------------------------------------------------------------------------------------------------------------------------------
#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
REQ_FILE="$PROJECT_DIR/requirements.txt"
SERVICE_NAME="media-controller.service"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"
RUN_USER="${SUDO_USER:-$USER}"

install_system_packages() {
  if command -v sudo >/dev/null 2>&1; then
    sudo apt update
    sudo apt install -y ffmpeg libsdl2-2.0-0 libsdl2-image-2.0-0 libsdl2-mixer-2.0-0 libsdl2-ttf-2.0-0 python3-venv
  else
    apt update
    apt install -y ffmpeg libsdl2-2.0-0 libsdl2-image-2.0-0 libsdl2-mixer-2.0-0 libsdl2-ttf-2.0-0 python3-venv
  fi
}

echo "[1/8] Checking python3..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install Python3 first."
  exit 1
fi

echo "[2/8] Installing system packages..."
if ! command -v ffplay >/dev/null 2>&1; then
  install_system_packages
fi

echo "[3/8] Creating virtual environment..."
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

echo "[4/8] Installing Python dependencies..."
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip
pip install -r "$REQ_FILE"
deactivate

echo "[5/8] Creating runtime folders/files..."
mkdir -p "$PROJECT_DIR/photos" "$PROJECT_DIR/videos"
[ -f "$PROJECT_DIR/config.json" ] || printf '{"photo_interval": 5}\n' > "$PROJECT_DIR/config.json"
[ -f "$PROJECT_DIR/pending_delete.json" ] || printf '[]\n' > "$PROJECT_DIR/pending_delete.json"

echo "[6/8] Disabling old conflicting services (if present)..."
for old_service in web-slideshow.service slideshow.service; do
  if systemctl list-unit-files | grep -q "^${old_service}"; then
    sudo systemctl stop "$old_service" || true
    sudo systemctl disable "$old_service" || true
  fi
done

echo "[7/8] Writing systemd service..."
sudo tee "$SERVICE_PATH" >/dev/null <<EOF
[Unit]
Description=Media Controller Flask App
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONUNBUFFERED=1
Environment=DISPLAY=:0
ExecStart=$VENV_DIR/bin/python $PROJECT_DIR/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo "[8/8] Enabling and starting service..."
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"

IP_ADDR="$(hostname -I | awk '{print $1}')"
echo
echo "Setup complete."
echo "Service status:"
sudo systemctl --no-pager --full status "$SERVICE_NAME" | sed -n '1,12p'
echo
echo "Open on phone/browser:"
echo "http://$IP_ADDR:5000"

