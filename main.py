#pip install flask requests beautifulsoup4 chardet urllib3 brotli lxml aiohttp
#https://mangaraw.best/manga/miichiyantoshan-tian-san
#https://kmansin09.org/mg/nano-mo-shena0/
from flask import Flask, request, Response, session
import requests
import aiohttp
import asyncio
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse, quote
import chardet
import mimetypes
import uuid
import urllib3
import re
import socket
import time
from functools import lru_cache
from flask_compress import Compress

app = Flask(__name__)
Compress(app)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app.secret_key = "supersecret"
sessions = {}

# ============ DNSキャッシング ============
_dns_cache = {}
_dns_cache_ttl = 300  # 5分

@lru_cache(maxsize=256)
def cached_getaddrinfo(host, port=None):
    """DNSキャッシング：同じホスト名は5分以内で再利用"""
    try:
        if port is None:
            return socket.getaddrinfo(host, 0)
        return socket.getaddrinfo(host, port)
    except:
        return None

def patched_getaddrinfo(host, port=0, family=0, socktype=0, proto=0, flags=0):
    """DNSキャッシュ付きgetaddrinfo"""
    try:
        return cached_getaddrinfo(host, port) or socket.getaddrinfo(host, port, family, socktype, proto, flags)
    except:
        return socket.getaddrinfo(host, port, family, socktype, proto, flags)

socket.getaddrinfo = patched_getaddrinfo

# ============ レスポンスキャッシング ============
_response_cache = {}
_cache_max_size = 100

def get_cache_key(url, method="GET"):
    """キャッシュキー生成"""
    return f"{method}:{url}"

def cache_get(key):
    """キャッシュから取得（TTLチェック付き）"""
    if key in _response_cache:
        entry = _response_cache[key]
        if time.time() - entry['time'] < entry['ttl']:
            return entry['data']
        else:
            del _response_cache[key]
    return None

def cache_set(key, data, ttl=3600):
    """キャッシュに保存"""
    if len(_response_cache) >= _cache_max_size:
        oldest = min(_response_cache.items(), key=lambda x: x[1]['time'])
        del _response_cache[oldest[0]]
    _response_cache[key] = {'data': data, 'time': time.time(), 'ttl': ttl}

# ============ 非同期CSS取得（複数並行処理） ============
async def fetch_css_content(session, css_url, headers):
    """単一CSSを非同期で取得して処理"""
    try:
        async with session.get(css_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10), ssl=False) as resp:
            if resp.status == 200:
                content_length = resp.content_length or 0
                css_content = await resp.read()
                encoding = resp.charset or "utf-8"
                css_text = css_content.decode(encoding, errors="replace")
                return {
                    "url": css_url,
                    "content": css_text,
                    "size": content_length,
                    "should_inline": content_length < 204800
                }
    except Exception:
        pass
    return {"url": css_url, "content": None, "size": 0, "should_inline": False}

async def process_css_links_async(css_urls, headers):
    """毎回新しいセッションを作成して複数CSSを並行取得"""
    connector = aiohttp.TCPConnector(ssl=False, limit=30)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            fetch_css_content(session, css_url, headers)
            for css_url in css_urls
        ]
        return await asyncio.gather(*tasks, return_exceptions=False)

# ============ ヘルパ ============
def get_user_session():
    uid = session.get("uid")
    if not uid:
        uid = str(uuid.uuid4())
        session["uid"] = uid
    if uid not in sessions:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "*/*",
            "Accept-Language": "ja,en-US;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        })
        # コネクションプール最適化：pool_maxsizeを100に増加
        adapter = requests.adapters.HTTPAdapter(pool_connections=30,
                                                pool_maxsize=100,
                                                max_retries=2)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        sessions[uid] = s
    return sessions[uid]


def detect_encoding(resp):
    ct = resp.headers.get("Content-Type", "")
    if "charset=" in ct and resp.encoding:
        return resp.encoding
    enc = chardet.detect(resp.content)["encoding"]
    return enc or "utf-8"


SCHEME_FIX_RE = re.compile(r'^(https?):/{3,}', re.IGNORECASE)
MULTISLASH_RE = re.compile(r'(?<!:)/{2,}')
WS_RE = re.compile(r'\s+')

def normalize_input_url(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    u = WS_RE.sub(" ", u).strip().strip('\'"')
    if u.startswith("//"):
        u = "https:" + u
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9+\-.]*://', u):
        u = "https://" + u.lstrip("/")
    u = SCHEME_FIX_RE.sub(r'\1://', u)
    pu = urlparse(u)
    fixed = pu.scheme + "://" + pu.netloc + MULTISLASH_RE.sub("/", pu.path)
    if pu.params:
        fixed += ";" + pu.params
    if pu.query:
        fixed += "?" + pu.query
    if pu.fragment:
        fixed += "#" + pu.fragment
    return fixed


def abs_url(base, val):
    if not val or isinstance(val, bytes) or str(val).startswith("data:"):
        return val
    v = str(val).strip().strip('"\'')
    if v.startswith("//"):
        v = "https:" + v
    if re.match(r'^(javascript:|mailto:|tel:|#)', v, re.IGNORECASE):
        return v
    return urljoin(base, v)


# ============ 正確な /go?url= 変換 ============
def make_proxy_url(url, base):
    if not url:
        return url
    absu = abs_url(base, url)
    if not absu or str(absu).startswith("data:"):
        return absu
    # 二重 /go?url= 防止
    if absu.startswith("/go?url=") or "/go?url=" in absu:
        return absu
    return "/go?url=" + quote(absu, safe='')


def stream_response(origin_resp):
    headers = {}
    for h in [
            "Content-Type", "Content-Length", "Content-Range", "Accept-Ranges",
            "ETag", "Last-Modified", "Cache-Control", "Content-Disposition"
    ]:
        if h in origin_resp.headers:
            headers[h] = origin_resp.headers[h]
    if "Cache-Control" not in headers:
        if headers.get("Content-Type", "").startswith("image/"):
            headers["Cache-Control"] = "public, max-age=86400"
        else:
            headers["Cache-Control"] = "no-transform, max-age=0"

    def generate():
        for chunk in origin_resp.iter_content(chunk_size=64 * 1024):
            if chunk:
                yield chunk

    return Response(generate(),
                    status=origin_resp.status_code,
                    headers=headers)


# ============ 次ページプリフェッチ検出 ============
def detect_next_page_url(soup, base_url):
    """次ページへのリンクを検出（漫画サイト最適化）"""
    next_keywords = [
        "次へ", "次ページ", "next", "次", "続きを読む", ">>",
        "→", "進む", "続き", "下へ", "next page"
    ]
    
    for a in soup.find_all("a", href=True):
        text = (a.get_text() or "").lower().strip()
        href = a.get("href")
        
        # キーワードマッチング（日本語と英語）
        for keyword in next_keywords:
            if keyword.lower() in text:
                next_url = abs_url(base_url, href)
                if next_url and not next_url.startswith("javascript:"):
                    return next_url
    
    # rel="next" 属性がある場合
    for link in soup.find_all("link", rel="next"):
        href = link.get("href")
        if href:
            return abs_url(base_url, href)
    
    return None

# ============ HTML 書き換え高速化 ============
def rewrite_html_urls(soup, base_url):
    tags_attrs = {
        "img": [
            "src", "data-src", "data-lazy-src", "data-original", "data-image",
            "data-file", "data-thumb", "srcset", "data-srcset"
        ],
        "iframe": ["src"],
        "form": ["action"]
    }

    def process_tag(t, tag, attr_list):
        if tag == "img":
            for lazy_attr in [
                    "data-src", "data-lazy-src", "data-original", "data-image",
                    "data-file", "data-thumb"
            ]:
                if t.has_attr(lazy_attr):
                    t["src"] = make_proxy_url(t.get(lazy_attr), base_url)
            if t.has_attr("loading"):
                del t["loading"]
        if t.has_attr("srcset") or t.has_attr("data-srcset"):
            attr_name = "srcset" if t.has_attr("srcset") else "data-srcset"
            srcset_val = t.get(attr_name)
            if srcset_val:
                new_srcset = []
                for item in srcset_val.split(","):
                    parts = item.strip().split()
                    if parts:
                        parts[0] = make_proxy_url(parts[0], base_url)
                        new_srcset.append(" ".join(parts))
                t[attr_name] = ", ".join(new_srcset)
        for attr in attr_list:
            if t.has_attr(attr):
                t[attr] = make_proxy_url(t.get(attr), base_url)
        if tag == "form":
            if not t.get("method"):
                t["method"] = "GET"
            act = t.get("action") or base_url
            t["action"] = "/go"
            hidden = soup.new_tag("input",
                                  attrs={
                                      "type": "hidden",
                                      "name": "url",
                                      "value": make_proxy_url(act, base_url)
                                  })
            t.append(hidden)

    for tag, attr_list in tags_attrs.items():
        for t in soup.find_all(tag):
            process_tag(t, tag, attr_list)

    # style 内の url() 書き換え
    bg_url_pattern = re.compile(r'url\(([^)]+)\)')
    for elem in soup.find_all(style=True):
        style_val = elem.get("style", "")
        def repl(m):
            raw = m.group(1).strip("\"'")
            return f"url({make_proxy_url(raw, base_url)})"
        elem["style"] = bg_url_pattern.sub(repl, style_val)

    # 全体 img 最大化
    style_tag = soup.new_tag("style")
    style_tag.string = "img{max-width:100%;height:auto;}"
    if soup.head:
        soup.head.append(style_tag)
    else:
        soup.insert(0, style_tag)
    # --- aタグを完全プロキシ化 ---
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if not href:
            continue
        if href.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        a["href"] = make_proxy_url(href, base_url)

    return soup


# ============ ルータ ============
REAL_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

# 削除するヘッダー（最小限のみ保持）
EXCLUDED_REQUEST_HEADERS = {
    "host", "connection", "cache-control", "upgrade-insecure-requests",
    "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"
}

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>", methods=["GET", "POST", "HEAD"])
def proxy(path):
    if not path:
        return '''
        <h2>URLを入力してください</h2>
        <form action="/go" method="get" style="margin:1rem 0;">
          <input name="url" placeholder="https://example.com" size="50" />
          <button type="submit">表示</button>
        </form>
        '''

    parsed_path = urlparse(path)
    if not parsed_path.scheme and not parsed_path.netloc:
        path = path.lstrip("/")
        path = "https://momon-ga.com/" + path
        parsed_path = urlparse(path)
    if not parsed_path.scheme:
        parsed_path = parsed_path._replace(scheme="https")
    target_url = urlunparse(parsed_path)

    user_session = get_user_session()
    
    # リクエストヘッダーを最小化（不要なヘッダーを削除）
    headers = {
        "User-Agent": REAL_CHROME_UA,
        "Referer": target_url,
        "Accept": request.headers.get("Accept", "*/*"),
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
    }
    
    try:
        resp = user_session.get(
            target_url,
            headers=headers,
            timeout=20,
            verify=False,
            allow_redirects=True,
        )

        content_type = resp.headers.get("Content-Type", "") or ""
        mime_hint = mimetypes.guess_type(target_url)[0]

        # ===============================
        # HTML
        # ===============================
        if "text/html" in content_type or mime_hint == "text/html":
            encoding = detect_encoding(resp)
            html = resp.content.decode(encoding, errors="replace")
            # lxmlパーサで高速化
            soup = BeautifulSoup(html, "lxml")

            # --- 画像 lazyload ---
            for img in soup.find_all("img"):
                for attr in [
                    "data-src", "data-original", "data-lazy",
                    "data-url", "data-img"
                ]:
                    if img.get(attr) and not img.get("src"):
                        img["src"] = img.get(attr)
                if img.get("srcset"):
                    del img["srcset"]
                if img.get("loading"):
                    del img["loading"]


            # --- CSS インライン（200KB以下）- 非同期並行処理 ---
            css_links = soup.find_all(
                "link", rel=lambda v: v and "stylesheet" in v
            )
            
            if css_links:
                # CSS URL抽出
                css_urls = []
                css_link_map = {}
                for link in css_links:
                    href = link.get("href")
                    if href:
                        css_url = abs_url(target_url, href)
                        css_urls.append(css_url)
                        css_link_map[css_url] = link
                
                # 非同期で複数CSS取得（並行処理）
                if css_urls:
                    try:
                        css_results = asyncio.run(
                            process_css_links_async(css_urls, headers)
                        )
                        
                        # 取得結果をHTMLに反映
                        for css_result in css_results:
                            link = css_link_map.get(css_result["url"])
                            if not link:
                                continue
                            
                            if css_result["content"] and css_result["should_inline"]:
                                # インライン化
                                style = soup.new_tag("style")
                                style.string = css_result["content"]
                                link.replace_with(style)
                            else:
                                # プロキシ経由
                                link["href"] = make_proxy_url(css_result["url"], target_url)
                    except:
                        # フォールバック：全てプロキシ経由
                        for link in css_links:
                            href = link.get("href")
                            if href:
                                css_url = abs_url(target_url, href)
                                link["href"] = make_proxy_url(css_url, target_url)

            # jQuery競合対策スクリプトを挿入
            protect_script = soup.new_tag("script")
            protect_script.string = """
            (function() {
                // jQueryが既に存在する場合にバックアップ＆保護
                if (typeof window.jQuery !== 'undefined') {
                    window.__realjQuery = window.jQuery;
                    window.__real$ = window.$;
                }
                // noConflictが呼ばれた後でもjQueryが使えるように
                window.addEventListener('DOMContentLoaded', function() {
                    if (typeof window.jQuery === 'undefined' && window.__realjQuery) {
                        window.jQuery = window.__realjQuery;
                        window.$ = window.__real$;
                    }
                }, {once: true});
            })();
            """
            # headの最初に挿入（なるべく早い段階で）
            if soup.head:
                soup.head.insert(0, protect_script)
            elif soup.body:
                soup.body.insert(0, protect_script)
            else:
                soup.insert(0, protect_script)

            soup = rewrite_html_urls(soup, target_url)

            # ============ 次ページプリフェッチ（漫画サイト向け） ============
            next_page_url = detect_next_page_url(soup, target_url)
            if next_page_url:
                prefetch_script = soup.new_tag("script")
                proxy_next_url = make_proxy_url(next_page_url, target_url)
                prefetch_script.string = f"""
                (function() {{
                    // 次ページを背後でプリフェッチ
                    try {{
                        var link = document.createElement('link');
                        link.rel = 'prefetch';
                        link.href = '{proxy_next_url}';
                        document.head.appendChild(link);
                    }} catch(e) {{}}
                    
                    // フォールバック: fetch APIで先読み
                    if (window.requestIdleCallback) {{
                        requestIdleCallback(function() {{
                            fetch('{proxy_next_url}', {{mode: 'no-cors'}}).catch(function() {{}});
                        }});
                    }} else {{
                        setTimeout(function() {{
                            fetch('{proxy_next_url}', {{mode: 'no-cors'}}).catch(function() {{}});
                        }}, 2000);
                    }}
                }})();
                """
                if soup.head:
                    soup.head.append(prefetch_script)
                elif soup.body:
                    soup.body.append(prefetch_script)
                else:
                    soup.append(prefetch_script)

            return Response(str(soup), content_type="text/html; charset=utf-8")

        # リソース用ヘッダー（最小化）
        resource_headers = {
            "User-Agent": REAL_CHROME_UA,
            "Accept": "*/*",
            "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
        }

        if request.headers.get("Cookie"):
            resource_headers["Cookie"] = request.headers["Cookie"]

        method = request.method if request.method != "HEAD" else "GET"
        r = user_session.request(
            method,
            target_url,
            headers=resource_headers,
            stream=True,
            timeout=30,
            verify=False,
            allow_redirects=True,
        )

        excluded_headers = {
            "content-encoding",
            "transfer-encoding",
            "connection",
            "content-security-policy",
            "strict-transport-security",
        }

        response_headers = [
            (k, v) for k, v in r.headers.items()
            if k.lower() not in excluded_headers
        ]

        if not any(h[0].lower() == "content-type" for h in response_headers):
            guessed = mimetypes.guess_type(target_url)[0]
            if guessed:
                response_headers.append(("Content-Type", guessed))

        def generate():
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

        return Response(
            generate(),
            status=r.status_code,
            headers=response_headers,
            direct_passthrough=True,
        )

    except Exception as e:
        return f"<h1>エラー</h1><pre>{e}</pre>", 500


@app.route("/go")
def redirect_form():
    raw = request.args.get("url", "")
    if not raw:
        return "URLが空です"
    url = normalize_input_url(raw)
    return proxy(url)

# ファイル末尾をこう書き換え
if __name__ == "__main__":
    # ローカル開発用（そのまま残してOK）
    app.run(host="0.0.0.0", port=3000, debug=True)
