"""Meta Model API client (Muse Spark text + Muse Image).

Reads the API key from the MODEL_API_KEY environment variable only —
keys are never stored in the repository. Endpoints mirror the OpenAI
API shape at https://api.meta.ai/v1:
  POST /v1/chat/completions   (muse-spark-1.3)
  POST /v1/images/generations (muse-image-1.0)
  POST /v1/images/edits       (anchored composition / refinement)
"""
from __future__ import annotations

import base64
import json
import os
import re

import requests

BASE_URL = os.environ.get("META_BASE_URL", "https://api.meta.ai/v1")
TEXT_MODEL = os.environ.get("META_TEXT_MODEL", "muse-spark-1.3")
IMAGE_MODEL = os.environ.get("META_IMAGE_MODEL", "muse-image-1.0")


class MetaError(RuntimeError):
    pass


class MetaClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("MODEL_API_KEY", "")
        self.session = requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self, json_content: bool = True) -> dict:
        h = {"Authorization": f"Bearer {self.api_key}"}
        if json_content:
            h["Content-Type"] = "application/json"
        return h

    # -- Muse Spark: text / structured output ------------------------------
    def chat(self, system: str, user: str, max_tokens: int = 2500) -> str:
        if not self.configured:
            raise MetaError("MODEL_API_KEY is not set")
        r = self.session.post(
            f"{BASE_URL}/chat/completions", headers=self._headers(),
            json={"model": TEXT_MODEL,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                  "max_tokens": max_tokens},
            timeout=90)
        if r.status_code != 200:
            raise MetaError(f"chat {r.status_code}: {r.text[:300]}")
        content = r.json()["choices"][0]["message"].get("content")
        if not content:
            raise MetaError("empty chat response (reasoning budget?)")
        return content

    def chat_json(self, system: str, user: str) -> dict | list | None:
        text = self.chat(system + "\nRespond with valid JSON only, "
                                  "no markdown fences.", user)
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M)
        try:
            return json.loads(text)
        except Exception:
            m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
            if m:
                try:
                    return json.loads(m.group(1))
                except Exception:
                    return None
            return None

    # -- Muse Image -----------------------------------------------------------
    def generate_image(self, prompt: str) -> bytes:
        if not self.configured:
            raise MetaError("MODEL_API_KEY is not set")
        r = self.session.post(
            f"{BASE_URL}/images/generations", headers=self._headers(),
            json={"model": IMAGE_MODEL, "prompt": prompt, "n": 1},
            timeout=240)
        if r.status_code != 200:
            raise MetaError(f"image {r.status_code}: {r.text[:300]}")
        return self._image_bytes(r.json())

    def edit_image(self, image_bytes: bytes, prompt: str,
                   filename: str = "anchor.webp") -> bytes:
        """Anchored composition: refine/reuse a reference image so a
        series (world -> poster) keeps the same style and subject."""
        if not self.configured:
            raise MetaError("MODEL_API_KEY is not set")
        r = self.session.post(
            f"{BASE_URL}/images/edits",
            headers=self._headers(json_content=False),
            data={"model": IMAGE_MODEL, "prompt": prompt, "n": "1"},
            files={"image": (filename, image_bytes, "image/webp")},
            timeout=240)
        if r.status_code != 200:
            raise MetaError(f"image edit {r.status_code}: {r.text[:300]}")
        return self._image_bytes(r.json())

    @staticmethod
    def _image_bytes(payload: dict) -> bytes:
        data = (payload.get("data") or [{}])[0]
        if data.get("b64_json"):
            return base64.b64decode(data["b64_json"])
        if data.get("url"):
            r = requests.get(data["url"], timeout=120)
            r.raise_for_status()
            return r.content
        raise MetaError(f"no image in response: {str(payload)[:200]}")


# -- Prompt builders (hybrid infographics: AI paints, code tells truth) ------

def world_prompt(project: dict) -> str:
    names = ", ".join(c["name"] for c in project["components"][:7]) or project["name"]
    return (
        "Pixel-art isometric floating-islands map of a software project, "
        f"one distinct island per module ({names}), connected by wooden "
        "bridges, tiny cozy buildings with warm lit windows, a small server "
        "tower, trees and circuits, dark navy night background, teal and "
        "amber palette, detailed retro game art. Calm empty water area in "
        "the lower third. Absolutely no text, no letters, no numbers, "
        "no signs, no logos."
    )


def poster_prompt(project: dict, activity: str) -> str:
    return (
        "The same pixel-art floating-islands world at a golden celebration "
        "moment: warm sunrise light, gentle confetti, bridges decorated "
        "with small lanterns, a tiny robot explorer waving a flag on the "
        "central island, joyful completion mood. Keep the same islands, "
        "style and palette as the reference image. Absolutely no text, "
        "no letters, no numbers, no logos."
    )
