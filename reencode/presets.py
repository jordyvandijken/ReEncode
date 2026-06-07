from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


_MEDIA_KEY_BY_TYPE = {
    "Images": "image",
    "Videos": "video",
    "Audio": "audio",
}


@dataclass(frozen=True)
class PresetMediaEntry:
    codec: str
    mode: str | None
    info: str | None


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    description: str
    media: dict[str, PresetMediaEntry]


def _presets_path() -> Path:
    return Path(__file__).with_name("presets.json")


def load_presets() -> list[Preset]:
    path = _presets_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    raw_items = payload.get("presets") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        return []

    parsed: list[Preset] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue

        preset_id = raw_item.get("id")
        name = raw_item.get("name")
        description = raw_item.get("description")
        raw_media = raw_item.get("media")
        if not isinstance(preset_id, str) or not isinstance(name, str) or not isinstance(description, str):
            continue
        if not isinstance(raw_media, dict):
            continue

        media: dict[str, PresetMediaEntry] = {}
        for key in ("image", "video", "audio"):
            value = raw_media.get(key)
            if not isinstance(value, dict):
                continue
            codec = value.get("codec")
            if not isinstance(codec, str) or not codec:
                continue
            mode = value.get("mode") if isinstance(value.get("mode"), str) else None
            info = value.get("info") if isinstance(value.get("info"), str) else None
            media[key] = PresetMediaEntry(codec=codec, mode=mode, info=info)

        parsed.append(Preset(id=preset_id, name=name, description=description, media=media))

    return parsed


def presets_by_id(presets: list[Preset]) -> dict[str, Preset]:
    return {preset.id: preset for preset in presets}


def media_entry_for_type(preset: Preset | None, media_type: str) -> PresetMediaEntry | None:
    if preset is None:
        return None
    media_key = _MEDIA_KEY_BY_TYPE.get(media_type)
    if not media_key:
        return None
    return preset.media.get(media_key)
