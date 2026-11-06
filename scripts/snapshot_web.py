"""从运行中的后台抓取页面，把 CSS 内联后存成可离线打开的快照。

用法:
    python scripts/snapshot_web.py [输出目录]
"""
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.getenv("SNAPSHOT_BASE", "http://127.0.0.1:8000")
OUT = sys.argv[1] if len(sys.argv) > 1 else ".preview"
USER = os.getenv("DEMO_MERCHANT_USER", "merchant")
PWD = os.getenv("DEMO_MERCHANT_PASSWORD", "admin123")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PAGES = [
    ("login.html", "/login", False),
    ("queue.html", "/queue", True),
    ("review.html", "/review/RF20261106093000E5F6", True),
    ("chat.html", "/chat?sid=demo-web-u_1002", True),
    ("analytics.html", "/analytics", True),
    ("audit.html", "/audit", True),
    ("refunds.html", "/refunds", True),
    ("orders.html", "/orders", True),
]


def fetch(path, cookie=None):
    req = urllib.request.Request(BASE + path)
    if cookie:
        req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def login():
    data = urllib.parse.urlencode({"username": USER, "password": PWD}).encode()
    req = urllib.request.Request(BASE + "/login", data=data)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=30) as resp:
            raw = resp.headers.get("Set-Cookie")
    except urllib.error.HTTPError as exc:  # 303 落在异常分支
        raw = exc.headers.get("Set-Cookie")
    if not raw:
        raise SystemExit("登录失败：服务端没有下发会话 cookie")
    return raw.split(";")[0].strip()


def inline_css(html):
    with open(os.path.join(ROOT, "web", "static", "style.css"), encoding="utf-8") as fh:
        css = fh.read()
    return re.sub(r'<link[^>]*rel="stylesheet"[^>]*>',
                  "<style>\n" + css + "\n</style>", html, count=1)


def main():
    out = os.path.join(ROOT, OUT)
    os.makedirs(out, exist_ok=True)
    cookie = login()
    for name, path, need_auth in PAGES:
        html = fetch(path, cookie if need_auth else None)
        html = inline_css(html)
        with open(os.path.join(out, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(html)
        print(f"  {name:<16} {len(html):>7} 字节   {path}")


if __name__ == "__main__":
    main()
