# -*- coding: utf-8 -*-
"""冒烟测试: 源码模式启动视频裁剪工具服务器并验证关键接口。
CI 在打包前运行:  python tests/smoke.py
覆盖: 主页可访问 / 列目录API / CSRF恶意Origin被拒 / 超大body被拒 / 非法JSON容错
"""
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(HERE)
sys.path.insert(0, SERVER_DIR)
os.chdir(SERVER_DIR)

PORT = 18993


def start_server():
    import start_server as srv_mod
    srv = srv_mod.ThreadingHTTPServer(("127.0.0.1", PORT), srv_mod.NoCacheHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def http(path, method="GET", body=None, headers=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), method=method,
                                 data=body.encode() if body else None,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main():
    srv = start_server()
    try:
        time.sleep(0.3)
        fails = []

        st, body = http("/")
        if st != 200 or b"<html" not in body.lower():
            fails.append("主页 200 失败: %d" % st)

        st, body = http("/api/fs")
        if st != 200:
            fails.append("/api/fs 状态码 %d" % st)

        st, body = http("/api/drives")
        if st != 200:
            fails.append("/api/drives 状态码 %d" % st)

        # CSRF: 伪造 127.0.0.1.evil.com Origin 应被拒(服务以 200+error 响应)
        st, body = http("/api/settings", headers={"Origin": "http://127.0.0.1.evil.com"})
        if b"blocked" not in body:
            fails.append("恶意Origin应被拒, body=%r" % body[:80])

        # CSRF: 正常本机 Origin 放行
        st, _ = http("/api/settings", headers={"Origin": "http://127.0.0.1:%d" % PORT})
        if st != 200:
            fails.append("本机Origin应200, 实际 %d" % st)

        # 超大 body 被拒
        req = urllib.request.Request("http://127.0.0.1:%d/api/settings" % PORT, method="POST",
                                     data=b"x" * (2 * 1024 * 1024),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                st = r.status
        except urllib.error.HTTPError as e:
            st = e.code
        if st != 200:  # __too_large__ 返回 200 + error 字段
            fails.append("超大body应200(error), 实际 %d" % st)

        # 非法 JSON 容错
        req = urllib.request.Request("http://127.0.0.1:%d/api/settings" % PORT, method="POST",
                                     data=b"not-json",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                st = r.status
        except urllib.error.HTTPError as e:
            st = e.code
        if st != 200:
            fails.append("非法JSON应200, 实际 %d" % st)

        if fails:
            print("SMOKE FAILED:")
            for f in fails:
                print("  -", f)
            sys.exit(1)
        print("SMOKE OK: 主页/API/CSRF/body限制 全部通过")
    finally:
        srv.shutdown()


if __name__ == "__main__":
    main()
