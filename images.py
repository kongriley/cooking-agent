"""Pictures for ingredients, tools, dishes and recipe steps, cached on disk.

Ingredients come from TheMealDB's catalog of product shots (matched by name, since unknown names return a placeholder
rather than an error); anything else, and dishes, from the lead image of the Wikipedia article with that name.
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import urllib.parse
from pathlib import Path

import aiohttp
import anthropic

MEALDB_CATALOG_URL = "https://www.themealdb.com/api/json/v1/1/list.php?i=list"
MEALDB_IMAGE_URL = "https://www.themealdb.com/images/ingredients/{}-Small.png"
WIKIPEDIA_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"
# Wikipedia asks API clients to identify themselves.
HEADERS = {"User-Agent": "BasilCookingAgent/0.1 (local kitchen app)"}
TIMEOUT = aiohttp.ClientTimeout(total=8)
# Descriptors that don't change what the thing looks like, dropped before matching.
DESCRIPTORS = re.compile(
    r"\b(fresh|large|small|medium|ripe|chopped|minced|sliced|diced|cold|softened|melted|unsalted|salted|whole|"
    r"extra[- ]virgin|european[- ]style|kosher|organic|raw|dried|ground|plain|all[- ]purpose)\b"
)


class Images:
    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
        self.cache: dict[str, str | None] = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        self.catalog: dict[str, str] | None = None  # lowercased name -> catalog spelling

    async def url(self, name: str, kind: str) -> str | None:
        """An image URL for `name`, a kind of 'ingredient', 'tool' or 'dish'; None when nothing fits."""
        key = f"{kind}:{name.strip().lower()}"
        if key not in self.cache:
            try:
                async with aiohttp.ClientSession(headers=HEADERS, timeout=TIMEOUT) as http:
                    self.cache[key] = await self._find(http, name.strip(), kind)
            except (aiohttp.ClientError, TimeoutError):
                return None  # a network hiccup isn't an answer; try again next time
            self.cache_path.write_text(json.dumps(self.cache, indent=1))
        return self.cache[key]

    async def _find(self, http: aiohttp.ClientSession, name: str, kind: str) -> str | None:
        if kind == "ingredient":
            if match := await self._catalog_match(http, name):
                return MEALDB_IMAGE_URL.format(urllib.parse.quote(match))
        plain = " ".join(DESCRIPTORS.sub(" ", name.lower()).split()) or name
        for title in dict.fromkeys([name, plain, plain.split()[-1] if kind != "dish" else plain]):
            if url := await self._wikipedia(http, title):
                return url
        return None

    async def _catalog_match(self, http: aiohttp.ClientSession, name: str) -> str | None:
        if self.catalog is None:
            async with http.get(MEALDB_CATALOG_URL) as response:
                meals = (await response.json(content_type=None))["meals"]
            self.catalog = {m["strIngredient"].lower(): m["strIngredient"] for m in meals}
        lowered = name.lower()
        plain = " ".join(DESCRIPTORS.sub(" ", lowered).split())
        candidates = [lowered, plain, plain.removesuffix("s"), plain.removesuffix("es")]
        if plain:
            candidates.append(plain.split()[-1])  # 'bread flour' -> 'flour'
        # The catalog uses British spellings.
        candidates += [re.sub(r"\b(chili|chile)\b", "chilli", c) for c in candidates]
        return next((self.catalog[c] for c in candidates if c in self.catalog), None)

    async def _wikipedia(self, http: aiohttp.ClientSession, title: str) -> str | None:
        async with http.get(WIKIPEDIA_SUMMARY_URL.format(urllib.parse.quote(title.replace(" ", "_")))) as response:
            if response.status != 200:
                return None
            page = await response.json()
        if page.get("type") == "disambiguation":
            return None
        return page.get("thumbnail", {}).get("source")


COMMONS_SEARCH_URL = "https://commons.wikimedia.org/w/api.php"
OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"
# OpenAI's fastest high-quality image model; at medium it's quicker than gpt-image-2 at low (about 9 s vs 11 s a
# picture) and noticeably truer to the step.
GENERATED_MODEL = "gpt-image-2.5-flare"
VET_MODEL = "claude-opus-5-5"
# Generating takes ~10 s a picture; two at a time keeps a fresh plan's pictures arriving steadily without a burst.
MAX_CONCURRENT_PICTURES = 4


class StepPictures:
    """A picture for each recipe step, generated from the step's instruction. Results are cached on disk by step."""

    def __init__(self, cache_dir: Path, openai_key: str | None) -> None:
        self.cache_dir = cache_dir
        cache_dir.mkdir(exist_ok=True)
        self.index_path = cache_dir / "index.json"
        self.index: dict[str, str | None] = json.loads(self.index_path.read_text()) if self.index_path.exists() else {}
        self.openai_key = openai_key
        self.limit = asyncio.Semaphore(MAX_CONCURRENT_PICTURES)
        self.pending: dict[str, asyncio.Task] = {}  # the page asks for each picture twice (preload, then display)

    async def picture(self, dish: str, title: str, text: str) -> str | None:
        """A photo URL, or the file name of a generated illustration in `cache_dir`; None when neither is available."""
        key = hashlib.sha256(f"{dish}\n{title}\n{text}".encode()).hexdigest()[:20]
        if key in self.index:
            return self.index[key]
        if key not in self.pending:
            self.pending[key] = asyncio.create_task(self._find_and_keep(key, dish, title, text))
        # Shielded: a request that gives up (the page moved on) mustn't cancel a picture that's nearly made.
        return await asyncio.shield(self.pending[key])

    async def _find_and_keep(self, key: str, dish: str, title: str, text: str) -> str | None:
        """Runs as its own task, so the result is cached even if every request waiting on it has gone."""
        try:
            found = await self._find(key, dish, title, text)
        except (aiohttp.ClientError, TimeoutError, anthropic.APIError):
            logging.exception("couldn't find a step picture")
            return None  # not cached, so a later request tries again
        finally:
            self.pending.pop(key, None)
        self.index[key] = found
        self.index_path.write_text(json.dumps(self.index, indent=1))
        return found

    async def _find(self, key: str, dish: str, title: str, text: str) -> str | None:
        if self.openai_key is None:
            return None
        async with self.limit, aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=90)) as http:
            return await self._generate(http, key, dish, text)

    async def _generate(self, http: aiohttp.ClientSession, key: str, dish: str, text: str) -> str:
        prompt = (
            f"A realistic photo of one step of making {dish} at home: {text} Hands at work, home kitchen counter, natural"
            " daylight, seen from slightly above. No text, no logos."
        )
        body = {
            "model": GENERATED_MODEL, "prompt": prompt, "size": "1536x1024", "quality": "medium", "n": 1,
            "output_format": "webp", "output_compression": 80,  # a tenth the size of the default PNG
        }  # fmt: skip
        headers = {"Authorization": f"Bearer {self.openai_key}"}
        async with http.post(OPENAI_IMAGES_URL, json=body, headers=headers) as response:
            response.raise_for_status()
            data = await response.json()
        name = f"{key}.webp"
        (self.cache_dir / name).write_bytes(base64.b64decode(data["data"][0]["b64_json"]))
        return name
