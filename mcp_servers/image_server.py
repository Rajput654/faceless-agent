"""
mcp_servers/image_server.py
Fetches background images from Pexels, Pixabay, or Pollinations (free AI images).
"""
import os
import random
import requests
from pathlib import Path
from loguru import logger


class ImageMCPServer:
    def __init__(self):
        self.pexels_key = os.environ.get("PEXELS_API_KEY", "")
        self.pixabay_key = os.environ.get("PIXABAY_API_KEY", "")
        self.tools = {
            "fetch_images": self._fetch_images,
            "generate_ai_image": self._generate_ai_image,
        }

    def call(self, tool_name: str, **kwargs):
        if tool_name not in self.tools:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        return self.tools[tool_name](**kwargs)

    def _fetch_images(self, query: str, output_paths: list, width: int = 1080, height: int = 1920, **kwargs):
        """Fetch images from Pexels or Pixabay."""
        results = []
        for i, path in enumerate(output_paths):
            Path(path).parent.mkdir(parents=True, exist_ok=True)

            # Try Pexels first
            if self.pexels_key:
                result = self._fetch_pexels(query, path, width, height, i)
                if result:
                    results.append(result)
                    continue

            # Try Pixabay
            if self.pixabay_key:
                result = self._fetch_pixabay(query, path, i)
                if result:
                    results.append(result)
                    continue

            # Fallback: Pollinations AI (free, no key needed)
            result = self._fetch_pollinations(query, path, width, height, i)
            if result:
                results.append(result)
            else:
                results.append({"success": False, "path": path, "error": "All image sources failed"})

        return {"success": len(results) > 0, "images": results}

    def _fetch_pexels(self, query: str, path: str, width: int, height: int, page: int = 0):
        try:
            headers = {"Authorization": self.pexels_key}
            params = {
                "query": query,
                "orientation": "portrait",
                "size": "large",
                "per_page": 15,
                "page": (page // 15) + 1,
            }
            resp = requests.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            photos = data.get("photos", [])
            if not photos:
                return None

            photo = photos[page % len(photos)]
            img_url = photo.get("src", {}).get("portrait", photo.get("src", {}).get("large", ""))
            if not img_url:
                return None

            img_resp = requests.get(img_url, timeout=30)
            img_resp.raise_for_status()
            with open(path, "wb") as f:
                f.write(img_resp.content)

            logger.info(f"Pexels image saved: {path}")
            return {"success": True, "path": path, "source": "pexels", "url": img_url}
        except Exception as e:
            logger.warning(f"Pexels fetch failed: {e}")
            return None

    def _fetch_pixabay(self, query: str, path: str, page: int = 0):
        try:
            params = {
                "key": self.pixabay_key,
                "q": query,
                "image_type": "photo",
                "orientation": "vertical",
                "per_page": 20,
                "page": 1,
                "safesearch": "true",
            }
            resp = requests.get("https://pixabay.com/api/", params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("hits", [])
            if not hits:
                return None

            hit = hits[page % len(hits)]
            img_url = hit.get("largeImageURL", hit.get("webformatURL", ""))
            if not img_url:
                return None

            img_resp = requests.get(img_url, timeout=30)
            img_resp.raise_for_status()
            with open(path, "wb") as f:
                f.write(img_resp.content)

            logger.info(f"Pixabay image saved: {path}")
            return {"success": True, "path": path, "source": "pixabay", "url": img_url}
        except Exception as e:
            logger.warning(f"Pixabay fetch failed: {e}")
            return None

    def _fetch_pollinations(self, query: str, path: str, width: int = 1080, height: int = 1920, seed: int = 0):
        """Use Pollinations.ai for free AI image generation."""
        try:
            import urllib.parse
            encoded_query = urllib.parse.quote(f"{query}, cinematic, high quality, 4k, vertical video")
            url = f"https://image.pollinations.ai/prompt/{encoded_query}?width={width}&height={height}&seed={seed}&nologo=true"

            resp = requests.get(url, timeout=60)
            resp.raise_for_status()

            with open(path, "wb") as f:
                f.write(resp.content)

            logger.info(f"Pollinations AI image saved: {path}")
            return {"success": True, "path": path, "source": "pollinations", "url": url}
        except Exception as e:
            logger.warning(f"Pollinations fetch failed: {e}")
            return None

    def _generate_ai_image(self, prompt: str, output_path: str, width: int = 1080, height: int = 1920, **kwargs):
        """Generate AI image using Pollinations."""
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        result = self._fetch_pollinations(prompt, output_path, width, height, random.randint(0, 9999))
        return result or {"success": False, "error": "AI image generation failed"}
