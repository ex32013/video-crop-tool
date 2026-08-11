# -*- coding: utf-8 -*-
"""视频裁剪工具 — 本地服务器(start_server.py)

⚠️ 开发提醒: 每次改完 index.html / 本文件后, 浏览器可能缓存旧版导致改动"看不到"。
   - 本服务器已对所有响应加 Cache-Control: no-store(刷新即最新)。
   - 若仍看到旧版: Ctrl+F5 强制刷新 / F12→Network→Disable cache / 换无痕窗口。

功能:
  * 静态服务本目录(index.html = 视频裁剪工具)
  * GET  /api/fs?path=<目录>   列目录(文件夹+视频), path 缺省 = 本目录
  * GET  /api/drives           盘符列表
  * GET  /api/stream?path=<绝对路径>  任意路径视频(带 Range 206, 支持 seek)
  * POST /api/crop             用本地 ffmpeg 裁剪: {input,start,end,crop,audio,name,outDir}

用法:
  双击 start.bat  或  python start_server.py [端口]
  浏览器打开 http://127.0.0.1:8003/

⚠️ 安全: 本工具按设计可读写本机任意路径(/api/fs 浏览、/api/stream 读视频、/api/crop 写文件)。
  仅限本机使用: 默认绑定 127.0.0.1, 不要改成 0.0.0.0 部署到公网。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# PyInstaller 打包支持: frozen 时 __file__ 在临时 _MEIPASS, 数据文件(index.html)从那里取;
# 用户数据(上传/设置)存可执行文件所在目录, 便于便携。
if getattr(sys, "frozen", False):
    ROOT = os.path.dirname(os.path.abspath(sys.executable))      # exe 所在目录: 用户数据
    STATIC_ROOT = sys._MEIPASS                                   # 内嵌 index.html 等
else:
    ROOT = os.path.dirname(os.path.abspath(__file__))
    STATIC_ROOT = ROOT
SETTINGS_FILE = os.path.join(ROOT, "设置.json")


def get_settings():
    if os.path.isfile(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_settings(obj):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _auto_install_imageio_ffmpeg():
    """找不到 ffmpeg 时自动 pip 安装 imageio-ffmpeg。默认源 60s 超时失败 → 换清华镜像重试。只跑一次。
    frozen(PyInstaller)时: 优先用打包自带的 imageio_ffmpeg 二进制; 若未打包则提示。"""
    global _AUTO_INSTALLED
    if _AUTO_INSTALLED:
        return None
    _AUTO_INSTALLED = True
    # frozen 且已打包 imageio_ffmpeg → 直接用内置二进制
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        # 打包后无法再 pip install(解释器不在), 提示用户
        print("提示: 本程序未内置 ffmpeg。请安装 ffmpeg 或重新打包时加入 imageio-ffmpeg。")
        return None
    try:
        cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
               "--timeout", "60", "imageio-ffmpeg"]
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        if r.returncode != 0:
            # 默认源失败(常见于国内网络) → 换清华镜像
            cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                   "-i", "https://pypi.tuna.tsinghua.edu.cn/simple", "imageio-ffmpeg"]
            subprocess.run(cmd, capture_output=True, timeout=300)
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    except Exception:
        return None


_FFMPEG_CACHE = None


def _find_ffmpeg():
    """查找 ffmpeg: PATH → imageio_ffmpeg(已装) → 自动 pip 安装 → 常见安装位置。跨平台。
    结果缓存, 避免每次裁剪都重新探测。"""
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE
    c = shutil.which("ffmpeg")
    if c:
        _FFMPEG_CACHE = c
        return c
    try:
        import imageio_ffmpeg
        _FFMPEG_CACHE = imageio_ffmpeg.get_ffmpeg_exe()
        return _FFMPEG_CACHE
    except Exception:
        pass
    # 自动安装依赖(60s 失败换清华镜像)
    exe = _auto_install_imageio_ffmpeg()
    if exe:
        _FFMPEG_CACHE = exe
        return exe
    cands = [
        os.path.join(os.environ.get("ProgramFiles", ""), "ffmpeg", "bin", "ffmpeg.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "ffmpeg", "bin", "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ]
    for p in cands:
        if p and os.path.exists(p):
            _FFMPEG_CACHE = p
            return p
    return None


_AUTO_INSTALLED = False


def list_drives():
    import string
    out = []
    for d in string.ascii_uppercase:
        p = d + ":\\"
        if os.path.exists(p):
            out.append(p)
    return out


def _downloads_dir():
    """浏览器下载目录(Windows): 优先 KnownFolder API, 退回 ~/Downloads / ~/下载。"""
    home = os.path.expanduser("~")
    for name in ("Downloads", "下载"):
        p = os.path.join(home, name)
        if os.path.isdir(p):
            return p
    try:
        import ctypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                        ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

        FOLDERID_Downloads = GUID(0x374DE290, 0x123F, 0x4565,
                                  (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
        fn = ctypes.windll.shell32.SHGetKnownFolderPath
        fn.argtypes = [ctypes.POINTER(GUID), ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        fn.restype = ctypes.c_long
        p = ctypes.c_wchar_p()
        if fn(ctypes.byref(FOLDERID_Downloads), 0, None, ctypes.byref(p)) == 0 and p.value:
            return p.value
    except Exception:
        pass
    return os.path.join(home, "Downloads")


def handle_fs(qs):
    root = qs.get("path", [""])[0] if qs else ""
    if not root:
        root = ROOT
    root = os.path.normpath(root)
    if not os.path.isdir(root):
        return {"ok": False, "error": "不是目录: " + root}
    parent = os.path.dirname(root)
    if parent == root:
        parent = None
    entries = []
    try:
        names = sorted(os.listdir(root), key=lambda n: (not os.path.isdir(os.path.join(root, n)), n.lower()))
    except OSError as e:
        return {"ok": False, "error": str(e)}
    for n in names:
        full = os.path.join(root, n)
        try:
            is_dir = os.path.isdir(full)
        except OSError:
            continue
        entries.append({"name": n, "dir": is_dir, "path": full})
    return {"ok": True, "dir": root, "parent": parent, "entries": entries}


def _safe_name(name):
    return re.sub(r'[\\/:*?"<>|]+', "_", str(name or "").strip()).strip(" ._") or "clip"


def handle_upload(raw, fname):
    """POST /api/upload: 接收拖入的视频文件, 存到 上传/ 目录, 返回可导入路径。"""
    if not fname:
        return {"ok": False, "error": "缺少文件名"}
    if not raw:
        return {"ok": False, "error": "空文件"}
    if len(raw) > 4 * 1024 * 1024 * 1024:
        return {"ok": False, "error": "文件过大(>4GB)"}
    out_dir = os.path.join(ROOT, "上传")
    os.makedirs(out_dir, exist_ok=True)
    name = _safe_name(fname)
    stem, ext = os.path.splitext(name)
    if not ext:
        ext = ".mp4"
    path = os.path.join(out_dir, stem + ext)
    i = 2
    while os.path.exists(path):
        path = os.path.join(out_dir, "%s_%d%s" % (stem, i, ext))
        i += 1
    try:
        with open(path, "wb") as f:
            f.write(raw)
    except Exception as e:
        return {"ok": False, "error": "写入失败: " + str(e)}
    return {"ok": True, "path": path, "name": os.path.basename(path)}


def handle_reveal(path):
    """POST /api/reveal: 打开导出目录(Windows os.startfile / 其他 xdg-open)。"""
    if not path or not os.path.isdir(path):
        return {"ok": False, "error": "不是目录: " + str(path)}
    try:
        if os.name == "nt":
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def handle_crop(body):
    raw_in = str(body.get("input", "") or "").strip()
    if not raw_in:
        return {"ok": False, "error": "无效的视频路径"}
    src = os.path.normpath(raw_in) if os.path.isabs(raw_in) else raw_in
    if not os.path.exists(src):
        return {"ok": False, "error": "视频不存在: " + raw_in}
    try:
        start = float(body.get("start", 0))
        end = float(body.get("end", start + 3))
    except (TypeError, ValueError):
        return {"ok": False, "error": "时间参数无效"}
    if start < 0:
        return {"ok": False, "error": "开始时间不能为负数"}
    if end <= start:
        return {"ok": False, "error": "结束时间必须大于开始时间"}
    crop = body.get("crop") or [0, 0, 1920, 1080]
    try:
        x0, y0, x1, y1 = [int(v) for v in crop[:4]]
    except (TypeError, ValueError):
        return {"ok": False, "error": "选区参数无效"}
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return {"ok": False, "error": "裁剪选区无效"}
    audio = bool(body.get("audio", True))
    name = _safe_name(body.get("name", "clip"))
    fmt = str(body.get("format", "mp4") or "mp4").lstrip(".").lower()
    if fmt not in ("mp4", "mov", "mkv"):
        fmt = "mp4"
    codec = str(body.get("codec", "h264") or "h264").lower()
    qkey = str(body.get("quality", "medium") or "").lower()
    if codec == "av1":
        crf = {"high": 32, "medium": 38, "low": 44}.get(qkey, 38)
        vcodec = ["-c:v", "libaom-av1", "-crf", str(crf), "-b:v", "0", "-cpu-used", "6", "-row-mt", "1", "-pix_fmt", "yuv420p"]
    else:
        crf = {"high": 16, "medium": 20, "low": 28}.get(qkey, 20)
        vcodec = ["-c:v", "libx264", "-crf", str(crf), "-preset", "medium", "-pix_fmt", "yuv420p"]
    out_dir = body.get("outDir")
    if out_dir and os.path.isabs(str(out_dir)):
        out_dir = os.path.normpath(str(out_dir))
    else:
        out_dir = _downloads_dir()
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, name + "." + fmt)
    i = 2
    while os.path.exists(out):
        out = os.path.join(out_dir, "%s_%d.%s" % (name, i, fmt))
        i += 1
    ff = _find_ffmpeg()
    if not ff:
        return {"ok": False, "error": "未找到 ffmpeg(请安装或把 ffmpeg 加入 PATH)"}
    dur = round(max(0.1, end - start), 3)
    cmd = [ff, "-y", "-nostdin", "-i", src, "-ss", str(start), "-t", str(dur),
           "-vf", "crop=%d:%d:%d:%d" % (w, h, x0, y0)] + vcodec
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "160k"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", out]
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or b"").decode("utf-8", "replace")[-800:]
        return {"ok": False, "error": "ffmpeg 失败: " + err}
    rel = None
    try:
        rel = os.path.relpath(out, os.getcwd()).replace("\\", "/")
    except ValueError:
        rel = None
    if rel and not rel.startswith(".."):
        url = "/" + rel
    else:
        url = "/api/stream?path=" + urllib.parse.quote(out)
    return {"ok": True, "file": out, "url": url, "name": os.path.basename(out)}


class NoCacheHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _csrf_ok(self):
        """CSRF/DNS rebinding 防护: 校验 Origin/Referer 必须来自本机"""
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not origin:
            return True  # 非浏览器请求(命令行), 无 CSRF 风险
        from urllib.parse import urlparse
        try:
            host = urlparse(origin).hostname or ""
        except Exception:
            return False
        if host in ("127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"):
            return True
        if host and (host.startswith("127.") or host == "::1"):
            return True
        return False

    def _send_json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        rng = self.headers.get("Range")
        if parsed.path.startswith("/api/") and not self._csrf_ok():
            self._send_json({"ok": False, "error": "cross-origin request blocked"})
            return
        if parsed.path == "/api/drives":
            self._send_json({"drives": list_drives()})
            return
        if parsed.path == "/api/downloads":
            self._send_json({"downloads": _downloads_dir()})
            return
        if parsed.path == "/api/settings":
            self._send_json(get_settings())
            return
        if parsed.path == "/api/fs":
            self._send_json(handle_fs(qs))
            return
        if parsed.path == "/api/stream":
            fpath = os.path.normpath(qs.get("path", [""])[0]) if qs else ""
            if not os.path.isfile(fpath):
                self.send_error(404)
                return
            # Range 请求优先; 失败(已发头)则不再二次响应
            if rng and rng.startswith("bytes="):
                if self._serve_file(fpath, rng):
                    return
                if self._response_started():
                    return
            self._serve_file(fpath, None)
            return
        if rng and rng.startswith("bytes="):
            if self._serve_range(rng):
                return
        super().do_GET()

    def _response_started(self):
        """是否已开始发送响应(防止二次响应)"""
        return getattr(self, "_resp_started", False)

    def _serve_file(self, fpath, rng):
        try:
            size = os.path.getsize(fpath)
            start, end = 0, size - 1
            code = 200
            if rng:
                spec = rng[6:].strip()
                if "-" not in spec:
                    return False
                a, b = spec.split("-", 1)
                if a:
                    try:
                        start = int(a)
                    except ValueError:
                        return False
                    end = int(b) if b else size - 1
                elif b:
                    # bytes=-N (最后 N 字节)
                    try:
                        n = int(b)
                    except ValueError:
                        return False
                    if n <= 0:
                        return False
                    start = max(0, size - n)
                    end = size - 1
                else:
                    return False
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return True
                end = min(max(end, start), size - 1)
                code = 206
            length = end - start + 1
            self.send_response(code)
            self.send_header("Accept-Ranges", "bytes")
            if code == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Type", self.guess_type(fpath))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
            self._resp_started = True
            with open(fpath, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            return True
        except Exception:
            return False

    def _serve_range(self, rng):
        try:
            path = urllib.parse.unquote(self.path.split("?", 1)[0])
            if path.startswith("/"):
                path = path[1:]
            path = path.replace("\\", "/")
            if ".." in path.split("/"):
                return False
            fpath = os.path.normpath(os.path.join(STATIC_ROOT, path))
            if not os.path.isfile(fpath):
                return False
            return self._serve_file(fpath, rng)
        except Exception:
            return False

    def do_POST(self):
        if not self._csrf_ok():
            self._send_json({"ok": False, "error": "cross-origin request blocked"})
            return
        path = self.path.rstrip("/")
        if path == "/api/crop":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
                self._send_json(handle_crop(body))
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)})
            return
        if path == "/api/settings":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
                self._send_json({"ok": save_settings(body)})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)})
            return
        if path == "/api/upload":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > 4 * 1024 * 1024 * 1024:
                    self._send_json({"ok": False, "error": "文件过大(>4GB)"})
                    return
                raw = self.rfile.read(length)
                fname = urllib.parse.unquote(self.headers.get("X-Filename", ""))
                self._send_json(handle_upload(raw, fname))
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)})
            return
        if path == "/api/reveal":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
                self._send_json(handle_reveal(body.get("path")))
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)})
            return
        self.send_error(405)


if __name__ == "__main__":
    import webbrowser
    os.chdir(STATIC_ROOT)
    host = "127.0.0.1"
    port = 8003
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    # 端口占用自动顺延(最多 +20)
    server = None
    for _ in range(20):
        try:
            server = ThreadingHTTPServer((host, port), NoCacheHandler)
            break
        except OSError:
            port += 1
    if server is None:
        print("错误: 端口 8003~8022 均被占用, 无法启动")
        sys.exit(1)
    server.daemon_threads = True
    print(f"视频裁剪工具已启动: http://{host}:{port}/  (根目录: {ROOT})")
    print("按 Ctrl+C 停止")
    if "--no-open" not in sys.argv:
        try:
            webbrowser.open(f"http://{host}:{port}/")
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已关闭")
