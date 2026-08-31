"""探测 SanDisk / Micron 官方 IR RSS 端点。"""
import httpx

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TraceEventRadar/0.2"}

candidates = [
    # SanDisk（2025 年从 Western Digital 分拆）
    "https://investor.sandisk.com/rss/news-releases.xml",
    "https://investor.sandisk.com/rss/press-releases.xml",
    "https://investor.sandisk.com/news-releases/rss",
    "https://investors.sandisk.com/rss/news-releases.xml",
    # Micron 替代尝试（带浏览器 UA）
    "https://investors.micron.com/rss.xml",
    "https://investors.micron.com/rss/news-releases.xml",
    "https://investors.micron.com/financial-information/sec-filings/rss",
]

for url in candidates:
    try:
        r = httpx.get(url, headers=UA, timeout=20, follow_redirects=True)
        is_feed = ("<item>" in r.text or "<entry>" in r.text) if r.status_code == 200 else False
        print(f"{r.status_code} feed={is_feed} {url}")
        if is_feed:
            import re
            titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", r.text)
            print("   first titles:", titles[1:3])
    except Exception as e:
        print(f"ERR {type(e).__name__} {url}")
