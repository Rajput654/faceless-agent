"""
mcp_servers/scraper_server.py
Web scraping utilities using Playwright and BeautifulSoup.
"""
import re
import requests
from loguru import logger

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False


class ScraperMCPServer:
    def __init__(self):
        self.tools = {
            "scrape_url": self._scrape_url,
            "extract_text": self._extract_text,
        }

    def call(self, tool_name: str, **kwargs):
        if tool_name not in self.tools:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        return self.tools[tool_name](**kwargs)

    def _scrape_url(self, url: str, **kwargs):
        try:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; faceless-agent/1.0)"}
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            return {"success": True, "html": resp.text, "status_code": resp.status_code}
        except Exception as e:
            logger.warning(f"Scrape failed for {url}: {e}")
            return {"success": False, "error": str(e)}

    def _extract_text(self, html: str, **kwargs):
        if not BS4_AVAILABLE:
            return {"success": False, "error": "beautifulsoup4 not installed"}
        try:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
            text = re.sub(r"\s+", " ", text).strip()
            return {"success": True, "text": text[:5000]}
        except Exception as e:
            return {"success": False, "error": str(e)}
