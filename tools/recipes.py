#!/usr/bin/env python3
"""Recipe catalog CLI for LARP feasts.

Plain-file store: one JSON file per recipe under recipes/, validated against
schema/recipe.schema.json. No database, no server, standard library only.

Subcommands:
  import-paprika   Import a .paprikarecipes archive (or single .paprikarecipe)
  import-jsonld    Import a schema.org Recipe (bare JSON-LD or an HTML page)
  export-paprika   Write a .paprikarecipes archive Paprika 3 can import
  validate         Check recipe files against the schema and house rules
  list             List recipes, with optional filters
  new              Scaffold an empty recipe file to fill in by hand

Run `tools/recipes.py --help` or `tools/recipes.py <subcommand> --help`.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import gzip
import hashlib
import html
import io
import json
import re
import sys
import unicodedata
import uuid
import zipfile
from html.parser import HTMLParser
from pathlib import Path

# ---------------------------------------------------------------------------
# Canonical format
# ---------------------------------------------------------------------------

# Field order used when writing recipe files, so diffs stay tidy.
FIELD_ORDER = [
    "slug", "name", "description", "ingredients", "directions",
    "servings", "prep_time", "cook_time", "total_time", "difficulty",
    "categories", "source", "source_url", "image_url", "image",
    "notes", "nutritional_info", "rating", "created", "paprika_uid",
    # LARP-specific
    "course", "base_headcount", "allergens", "period_notes", "tags",
]

STRING_FIELDS = {
    "description", "servings", "prep_time", "cook_time", "total_time",
    "difficulty", "source", "source_url", "image_url", "notes",
    "nutritional_info", "period_notes",
}
LIST_FIELDS = {"ingredients", "directions", "categories", "allergens", "tags"}
# Optional scalars that are omitted from the file when unset.
OPTIONAL_FIELDS = {"image", "paprika_uid", "course", "base_headcount"}
# Fields that only exist in this catalog, never in Paprika. Preserved when a
# recipe is re-imported with --update.
LARP_FIELDS = ["course", "base_headcount", "allergens", "period_notes", "tags"]

# Stable namespace for deriving a Paprika uid from a slug when a recipe never
# came from Paprika. Re-exporting then updates rather than duplicates.
PAPRIKA_UID_NAMESPACE = uuid.UUID("a0b4b4b0-5a86-4e2e-9a2e-5f1b3c2d1e0f")

UNICODE_FRACTIONS = {
    "¼": "1/4", "½": "1/2", "¾": "3/4",
    "⅐": "1/7", "⅑": "1/9", "⅒": "1/10",
    "⅓": "1/3", "⅔": "2/3",
    "⅕": "1/5", "⅖": "2/5", "⅗": "3/5", "⅘": "4/5",
    "⅙": "1/6", "⅚": "5/6",
    "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8",
}
_FRACTION_CHARS = "".join(UNICODE_FRACTIONS)


class RecipeError(Exception):
    """A user-facing error; the CLI prints the message and exits non-zero."""


def empty_recipe(slug: str, name: str) -> dict:
    """A recipe dict with every core field present and empty."""
    r = {"slug": slug, "name": name}
    for f in FIELD_ORDER:
        if f in r or f in OPTIONAL_FIELDS:
            continue
        if f in LIST_FIELDS:
            r[f] = []
        elif f == "rating":
            r[f] = 0
        elif f == "created":
            r[f] = dt.date.today().isoformat()
        else:
            r[f] = ""
    return r


def normalize_recipe(recipe: dict) -> dict:
    """Return a copy in canonical key order with unset optionals dropped."""
    out = {}
    for f in FIELD_ORDER:
        if f not in recipe:
            continue
        v = recipe[f]
        if v is None:
            continue
        if f in OPTIONAL_FIELDS and v == "":
            continue
        out[f] = v
    # Keep unknown keys at the end so validate can report them rather than
    # silently discarding data.
    for k, v in recipe.items():
        if k not in out and k not in FIELD_ORDER:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "recipe"


def plain_fractions(text: str) -> str:
    """Replace Unicode fraction characters with ASCII ('1½' -> '1 1/2')."""
    if not text:
        return text
    text = text.replace("⁄", "/")  # fraction slash
    text = re.sub(rf"(\d)([{_FRACTION_CHARS}])", r"\1 \2", text)
    for ch, ascii_frac in UNICODE_FRACTIONS.items():
        text = text.replace(ch, ascii_frac)
    return text


def has_unicode_fraction(text: str) -> bool:
    return any(ch in text for ch in UNICODE_FRACTIONS) or "⁄" in text


def split_lines(text: str | None) -> list[str]:
    """Split newline-separated text into stripped, non-empty lines."""
    if not text:
        return []
    return [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_TAG_RE = re.compile(r"<\s*/?\s*(?:p|br|li|div|h\d|tr)\b[^>]*>", re.I)


def strip_html(text: str) -> str:
    """Turn an HTML fragment into plain text, block tags becoming newlines."""
    if not text:
        return ""
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def clean_text(value) -> str:
    """Single-line plain text from a JSON-LD string-ish value."""
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(clean_text(v) for v in value if v)
    if isinstance(value, dict):
        return clean_text(value.get("name") or value.get("text") or value.get("@value") or "")
    return " ".join(strip_html(str(value)).split())


_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


def human_duration(value) -> str:
    """ISO 8601 duration (PT1H30M) -> '1 hr 30 min'. Other text passes through."""
    if value is None:
        return ""
    if isinstance(value, list):
        value = value[0] if value else ""
    text = str(value).strip()
    m = _ISO_DURATION_RE.match(text.upper())
    if not m or text.upper() == "P":
        return text
    minutes = 0.0
    minutes += float(m.group("days") or 0) * 24 * 60
    minutes += float(m.group("hours") or 0) * 60
    minutes += float(m.group("minutes") or 0)
    minutes += float(m.group("seconds") or 0) / 60
    minutes = int(round(minutes))
    if minutes <= 0:
        return ""
    hours, mins = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hr" if hours == 1 else f"{hours} hrs")
    if mins:
        parts.append(f"{mins} min")
    return " ".join(parts)


def as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


# ---------------------------------------------------------------------------
# Store (recipes/ directory)
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.recipes_dir = self.root / "recipes"
        self.images_dir = self.root / "images"
        self.schema_path = self.root / "schema" / "recipe.schema.json"

    def path_for(self, slug: str) -> Path:
        return self.recipes_dir / f"{slug}.json"

    def paths(self) -> list[Path]:
        if not self.recipes_dir.is_dir():
            return []
        return sorted(p for p in self.recipes_dir.glob("*.json") if p.is_file())

    def load(self, path: Path) -> dict:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise RecipeError(f"{path}: not valid JSON ({exc})") from exc
        if not isinstance(data, dict):
            raise RecipeError(f"{path}: top level must be a JSON object")
        return data

    def load_all(self) -> list[dict]:
        return [self.load(p) for p in self.paths()]

    def save(self, recipe: dict) -> Path:
        self.recipes_dir.mkdir(parents=True, exist_ok=True)
        recipe = normalize_recipe(recipe)
        path = self.path_for(recipe["slug"])
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(recipe, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        return path

    def exists(self, slug: str) -> bool:
        return self.path_for(slug).is_file()

    def unique_slug(self, base: str) -> str:
        """First free slug in base, base-2, base-3, ..."""
        slug, n = base, 1
        while self.exists(slug):
            n += 1
            slug = f"{base}-{n}"
        return slug

    def find_by_paprika_uid(self, uid: str) -> dict | None:
        if not uid:
            return None
        for r in self.load_all():
            if r.get("paprika_uid", "").upper() == uid.upper():
                return r
        return None

    def save_image(self, slug: str, data: bytes, original_name: str | None) -> str:
        ext = Path(original_name or "").suffix.lower() or ".jpg"
        if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            ext = ".jpg"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        rel = f"images/{slug}{ext}"
        (self.root / rel).write_bytes(data)
        return rel

    def store_incoming(self, recipe: dict, *, update: bool, log) -> str | None:
        """Add an imported recipe, honouring existing files.

        Returns the slug written, or None if skipped. If a recipe with the
        same paprika_uid or slug already exists, it is only replaced when
        update=True, and then its LARP-only fields are preserved.
        """
        existing = self.find_by_paprika_uid(recipe.get("paprika_uid", ""))
        if existing is None and self.exists(recipe["slug"]):
            existing = self.load(self.path_for(recipe["slug"]))
        if existing is not None:
            if not update:
                log(f"  skip   {existing['slug']}  (already exists; use --update to replace)")
                return None
            recipe["slug"] = existing["slug"]
            for f in LARP_FIELDS:
                if f in existing and f not in recipe:
                    recipe[f] = existing[f]
                elif f in existing and existing[f] and not recipe.get(f):
                    recipe[f] = existing[f]
            if "image" in existing and "image" not in recipe:
                recipe["image"] = existing["image"]
            if existing.get("created"):
                recipe["created"] = existing["created"]
            self.save(recipe)
            log(f"  update {recipe['slug']}  {recipe['name']}")
            return recipe["slug"]
        recipe["slug"] = self.unique_slug(recipe["slug"])
        self.save(recipe)
        log(f"  add    {recipe['slug']}  {recipe['name']}")
        return recipe["slug"]


def default_root() -> Path:
    return Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Paprika 3 format
# ---------------------------------------------------------------------------

PAPRIKA_TEXT_FIELDS = [
    "description", "notes", "nutritional_info", "servings", "difficulty",
    "prep_time", "cook_time", "total_time", "source", "source_url", "image_url",
]


def paprika_to_recipe(p: dict) -> dict:
    """Convert one decoded .paprikarecipe JSON object to the canonical format.

    Unknown Paprika fields (hash, scale, on_favorites, photo_large, ...) are
    ignored. The photo, if any, is returned under the private key "_photo"
    as (bytes, filename) for the caller to write to images/.
    """
    name = (p.get("name") or "").strip() or "Untitled recipe"
    r = empty_recipe(slugify(name), name)
    r["ingredients"] = [plain_fractions(x) for x in split_lines(p.get("ingredients"))]
    r["directions"] = [plain_fractions(x) for x in split_lines(p.get("directions"))]
    for f in PAPRIKA_TEXT_FIELDS:
        r[f] = (p.get(f) or "").strip() if isinstance(p.get(f), str) else ""
    r["categories"] = [str(c).strip() for c in as_list(p.get("categories")) if str(c).strip()]
    try:
        r["rating"] = max(0, min(5, int(p.get("rating") or 0)))
    except (TypeError, ValueError):
        r["rating"] = 0
    created = (p.get("created") or "").strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", created)
    r["created"] = m.group(1) if m else dt.date.today().isoformat()
    uid = (p.get("uid") or "").strip().upper()
    if uid:
        r["paprika_uid"] = uid
    photo_data = p.get("photo_data")
    if photo_data:
        try:
            r["_photo"] = (base64.b64decode(photo_data), p.get("photo"))
        except (ValueError, TypeError):
            pass
    return r


def read_paprika_archive(path: Path) -> list[dict]:
    """Decode a .paprikarecipes zip (or a bare .paprikarecipe) into dicts."""
    path = Path(path)
    if not path.is_file():
        raise RecipeError(f"{path}: file not found")
    raw = path.read_bytes()
    blobs: list[tuple[str, bytes]] = []
    if zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                blobs.append((info.filename, zf.read(info)))
    else:
        blobs.append((path.name, raw))

    out = []
    for name, blob in blobs:
        try:
            text = gzip.decompress(blob).decode("utf-8")
        except (OSError, EOFError, UnicodeDecodeError):
            # Tolerate uncompressed JSON entries too.
            try:
                text = blob.decode("utf-8")
            except UnicodeDecodeError:
                print(f"  warn   {name}: not a gzip/JSON entry, skipped", file=sys.stderr)
                continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            print(f"  warn   {name}: not valid JSON, skipped", file=sys.stderr)
            continue
        # A single entry may itself hold a list of recipes.
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict):
                out.append(item)
    return out


def recipe_to_paprika(recipe: dict, root: Path) -> dict:
    """Convert a canonical recipe to the JSON object Paprika 3 expects."""
    uid = recipe.get("paprika_uid") or str(uuid.uuid5(PAPRIKA_UID_NAMESPACE, recipe["slug"])).upper()
    created = recipe.get("created") or dt.date.today().isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", created):
        created = f"{created} 00:00:00"
    p = {
        "uid": uid,
        "name": recipe.get("name", ""),
        "ingredients": "\n".join(recipe.get("ingredients", [])),
        "directions": "\n\n".join(recipe.get("directions", [])),
        "description": recipe.get("description", ""),
        "notes": recipe.get("notes", ""),
        "nutritional_info": recipe.get("nutritional_info", ""),
        "servings": recipe.get("servings", ""),
        "difficulty": recipe.get("difficulty", ""),
        "prep_time": recipe.get("prep_time", ""),
        "cook_time": recipe.get("cook_time", ""),
        "total_time": recipe.get("total_time", ""),
        "source": recipe.get("source", ""),
        "source_url": recipe.get("source_url", ""),
        "image_url": recipe.get("image_url") or None,
        "categories": list(recipe.get("categories", [])),
        "rating": int(recipe.get("rating", 0) or 0),
        "photo": None,
        "photo_hash": None,
        "photo_data": None,
        "created": created,
    }
    image = recipe.get("image")
    if image:
        img_path = Path(root) / image
        if img_path.is_file():
            data = img_path.read_bytes()
            p["photo"] = img_path.name
            p["photo_hash"] = hashlib.sha256(data).hexdigest()
            p["photo_data"] = base64.b64encode(data).decode("ascii")
        else:
            print(f"  warn   {recipe['slug']}: image {image} not found, exported without photo",
                  file=sys.stderr)
    return p


def write_paprika_archive(recipes: list[dict], root: Path, out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for r in recipes:
            p = recipe_to_paprika(r, root)
            payload = gzip.compress(json.dumps(p, ensure_ascii=False).encode("utf-8"))
            zf.writestr(f"{r['slug']}.paprikarecipe", payload)
    return out_path


# ---------------------------------------------------------------------------
# schema.org Recipe (JSON-LD)
# ---------------------------------------------------------------------------

class _LdJsonExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self._in_block = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "script":
            t = dict(attrs).get("type") or ""
            if t.strip().lower() == "application/ld+json":
                self._in_block = True
                self._buf = []

    def handle_data(self, data):
        if self._in_block:
            self._buf.append(data)

    def handle_entityref(self, name):
        if self._in_block:
            self._buf.append(f"&{name};")

    def handle_charref(self, name):
        if self._in_block:
            self._buf.append(f"&#{name};")

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._in_block:
            self.blocks.append("".join(self._buf))
            self._in_block = False


def _parse_ld_block(text: str):
    text = text.strip()
    # Some sites wrap the JSON in an HTML comment or CDATA.
    text = re.sub(r"^<!--|-->$", "", text).strip()
    text = re.sub(r"^//\s*<!\[CDATA\[|//\s*\]\]>$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _is_recipe(node) -> bool:
    if not isinstance(node, dict):
        return False
    types = [str(t).rsplit("/", 1)[-1].lower() for t in as_list(node.get("@type"))]
    return "recipe" in types


def find_recipe_nodes(data) -> list[dict]:
    """Walk a JSON-LD document and return every schema.org Recipe object."""
    found: list[dict] = []

    def walk(node, depth=0):
        if depth > 12:
            return
        if isinstance(node, dict):
            if _is_recipe(node):
                found.append(node)
                return
            for key in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement", "hasPart"):
                if key in node:
                    walk(node[key], depth + 1)
            for key, value in node.items():
                if key.startswith("@") or key in ("mainEntity", "mainEntityOfPage",
                                                   "itemListElement", "hasPart"):
                    continue
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(data)
    # De-duplicate (the same node can be reachable twice).
    unique, seen = [], set()
    for n in found:
        if id(n) not in seen:
            seen.add(id(n))
            unique.append(n)
    return unique


def load_jsonld_file(path: Path) -> list[dict]:
    """Return Recipe nodes from a JSON file or an HTML page with ld+json."""
    path = Path(path)
    if not path.is_file():
        raise RecipeError(f"{path}: file not found")
    text = path.read_text(encoding="utf-8", errors="replace")
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        data = _parse_ld_block(text)
        if data is None:
            raise RecipeError(f"{path}: not valid JSON")
        return find_recipe_nodes(data)
    parser = _LdJsonExtractor()
    parser.feed(text)
    parser.close()
    nodes: list[dict] = []
    for block in parser.blocks:
        data = _parse_ld_block(block)
        if data is not None:
            nodes.extend(find_recipe_nodes(data))
    return nodes


def flatten_instructions(value) -> list[str]:
    """recipeInstructions in any of its common shapes -> list of paragraphs."""
    steps: list[str] = []

    def add_text(text):
        for line in strip_html(str(text)).split("\n"):
            line = line.strip()
            if line:
                steps.append(line)

    def walk(node):
        if node is None:
            return
        if isinstance(node, str):
            add_text(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            types = [str(t).lower() for t in as_list(node.get("@type"))]
            if "howtosection" in types or ("itemListElement" in node and "text" not in node):
                heading = clean_text(node.get("name"))
                if heading:
                    steps.append(heading.rstrip(":") + ":")
                walk(node.get("itemListElement"))
            else:
                # HowToStep, HowToDirection, or an untyped {text: ...}
                text = node.get("text") or node.get("description") or node.get("name") or ""
                if text:
                    add_text(text)
                elif "itemListElement" in node:
                    walk(node["itemListElement"])

    walk(value)
    return steps


def _yield_to_servings(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("value") or value.get("name") or ""
    if isinstance(value, list):
        strings = [clean_text(v) for v in value if v not in (None, "")]
        strings = [s for s in strings if s]
        if not strings:
            return ""
        # Prefer the entry that reads like a serving count over a bare number.
        for s in strings:
            if "serv" in s.lower():
                return s
        return strings[0]
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    return clean_text(value)


def _image_url(value) -> str:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl") or ""
    return str(value or "").strip()


def _author(value) -> str:
    if isinstance(value, list):
        names = [_author(v) for v in value]
        return ", ".join(n for n in names if n)
    if isinstance(value, dict):
        return clean_text(value.get("name"))
    return clean_text(value)


def _keywords(value) -> list[str]:
    if isinstance(value, str):
        return [k.strip() for k in value.split(",") if k.strip()]
    return [clean_text(v) for v in as_list(value) if clean_text(v)]


def _nutrition(value) -> str:
    if not isinstance(value, dict):
        return clean_text(value)
    parts = []
    for key, label in (("calories", "Calories"), ("fatContent", "Fat"),
                       ("carbohydrateContent", "Carbs"), ("proteinContent", "Protein"),
                       ("sodiumContent", "Sodium"), ("fiberContent", "Fiber"),
                       ("sugarContent", "Sugar")):
        if value.get(key):
            parts.append(f"{label}: {clean_text(value[key])}")
    return "; ".join(parts)


def jsonld_to_recipe(node: dict, source_url_hint: str = "") -> dict:
    name = clean_text(node.get("name")) or clean_text(node.get("headline")) or "Untitled recipe"
    r = empty_recipe(slugify(name), name)
    r["description"] = plain_fractions(clean_text(node.get("description")))
    r["ingredients"] = [plain_fractions(clean_text(i))
                        for i in as_list(node.get("recipeIngredient") or node.get("ingredients"))]
    r["ingredients"] = [i for i in r["ingredients"] if i]
    r["directions"] = [plain_fractions(s) for s in flatten_instructions(node.get("recipeInstructions"))]
    r["servings"] = _yield_to_servings(node.get("recipeYield"))
    r["prep_time"] = human_duration(node.get("prepTime"))
    r["cook_time"] = human_duration(node.get("cookTime"))
    r["total_time"] = human_duration(node.get("totalTime"))
    r["categories"] = []
    for key in ("recipeCategory", "recipeCuisine"):
        for c in as_list(node.get(key)):
            c = clean_text(c)
            if c and c not in r["categories"]:
                r["categories"].append(c)
    r["source"] = _author(node.get("author")) or clean_text(
        (node.get("publisher") or {}).get("name") if isinstance(node.get("publisher"), dict) else "")
    url = node.get("url") or node.get("mainEntityOfPage") or node.get("@id") or source_url_hint
    if isinstance(url, dict):
        url = url.get("@id") or url.get("url") or ""
    r["source_url"] = str(url or "").strip() if str(url or "").startswith("http") else ""
    r["image_url"] = _image_url(node.get("image"))
    r["nutritional_info"] = _nutrition(node.get("nutrition"))
    r["tags"] = _keywords(node.get("keywords"))
    return r


# ---------------------------------------------------------------------------
# Validation (small JSON Schema subset; standard library only)
# ---------------------------------------------------------------------------

_JSON_TYPES = {
    "object": dict, "array": list, "string": str, "integer": int,
    "number": (int, float), "boolean": bool, "null": type(None),
}


def _check_type(value, type_spec) -> bool:
    for t in as_list(type_spec):
        py = _JSON_TYPES.get(t)
        if py is None:
            continue
        if t in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, py):
            return True
    return False


def schema_errors(value, schema: dict, path: str = "$") -> list[str]:
    """Validate against the subset of JSON Schema used by recipe.schema.json."""
    errs: list[str] = []
    if "type" in schema and not _check_type(value, schema["type"]):
        errs.append(f"{path}: expected {schema['type']}, got {type(value).__name__}")
        return errs
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: must be one of {schema['enum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append(f"{path}: must not be empty")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errs.append(f"{path}: {value!r} does not match {schema['pattern']}")
        if schema.get("format") == "date":
            try:
                dt.date.fromisoformat(value)
            except ValueError:
                errs.append(f"{path}: {value!r} is not a valid date")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: {value} is below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: {value} is above maximum {schema['maximum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{path}: missing required field {req!r}")
        for key, item in value.items():
            if key in props:
                errs.extend(schema_errors(item, props[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: unknown field {key!r}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errs.extend(schema_errors(item, schema["items"], f"{path}[{i}]"))
    return errs


def house_rule_errors(recipe: dict, path: Path, store: Store) -> list[str]:
    """Checks beyond the schema: file name, fractions, image existence."""
    errs = []
    slug = recipe.get("slug")
    if isinstance(slug, str) and path.stem != slug:
        errs.append(f"slug {slug!r} does not match file name {path.name!r}")
    for field in ("ingredients", "directions"):
        for i, line in enumerate(recipe.get(field) or []):
            if isinstance(line, str) and has_unicode_fraction(line):
                errs.append(f"{field}[{i}]: use plain fractions like 1/2, not {line!r}")
    image = recipe.get("image")
    if isinstance(image, str) and image and not (store.root / image).is_file():
        errs.append(f"image {image!r} not found under {store.root}")
    return errs


def validate_store(store: Store, paths: list[Path] | None = None) -> dict[Path, list[str]]:
    schema = json.loads(store.schema_path.read_text(encoding="utf-8"))
    results: dict[Path, list[str]] = {}
    for path in (paths or store.paths()):
        try:
            recipe = store.load(path)
        except RecipeError as exc:
            results[path] = [str(exc)]
            continue
        errs = schema_errors(recipe, schema)
        errs += house_rule_errors(recipe, path, store)
        results[path] = errs
    return results


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def _ci_in(needle: str, haystack) -> bool:
    return needle.lower() in [str(x).lower() for x in as_list(haystack)]


def filter_recipes(recipes: list[dict], *, slugs=None, category=None, course=None, tag=None) -> list[dict]:
    out = []
    wanted = set(slugs or [])
    for r in recipes:
        if wanted and r.get("slug") not in wanted:
            continue
        if category and not _ci_in(category, r.get("categories")):
            continue
        if tag and not _ci_in(tag, r.get("tags")):
            continue
        if course and str(r.get("course", "")).lower() != course.lower():
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_import_paprika(args, store: Store) -> int:
    total = 0
    for file in args.files:
        print(f"{file}:")
        entries = read_paprika_archive(Path(file))
        if not entries:
            print("  (no recipes found)")
        for entry in entries:
            recipe = paprika_to_recipe(entry)
            photo = recipe.pop("_photo", None)
            for t in args.tag or []:
                if t not in recipe["tags"]:
                    recipe["tags"].append(t)
            slug = store.store_incoming(recipe, update=args.update, log=print)
            if slug is None:
                continue
            total += 1
            if photo:
                saved = store.load(store.path_for(slug))
                saved["image"] = store.save_image(slug, photo[0], photo[1])
                store.save(saved)
    print(f"Imported {total} recipe(s) into {store.recipes_dir}")
    return 0


def cmd_import_jsonld(args, store: Store) -> int:
    total = 0
    for file in args.files:
        print(f"{file}:")
        nodes = load_jsonld_file(Path(file))
        if not nodes:
            print("  (no schema.org Recipe found)")
            continue
        for node in nodes:
            recipe = jsonld_to_recipe(node, source_url_hint=args.source_url or "")
            if args.source_url and not recipe["source_url"]:
                recipe["source_url"] = args.source_url
            for t in args.tag or []:
                if t not in recipe["tags"]:
                    recipe["tags"].append(t)
            if store.store_incoming(recipe, update=args.update, log=print):
                total += 1
    print(f"Imported {total} recipe(s) into {store.recipes_dir}")
    return 0


def cmd_export_paprika(args, store: Store) -> int:
    recipes = filter_recipes(store.load_all(), slugs=args.slugs, category=args.category,
                             course=args.course, tag=args.tag)
    if args.slugs:
        missing = sorted(set(args.slugs) - {r["slug"] for r in recipes})
        if missing:
            raise RecipeError("no such recipe(s): " + ", ".join(missing))
    if not recipes:
        raise RecipeError("nothing to export (no recipes matched)")
    out = Path(args.output) if args.output else store.root / "build" / "recipes.paprikarecipes"
    write_paprika_archive(recipes, store.root, out)
    for r in recipes:
        print(f"  {r['slug']}")
    print(f"Wrote {len(recipes)} recipe(s) to {out}")
    return 0


def cmd_validate(args, store: Store) -> int:
    paths = [Path(p) for p in args.files] if args.files else None
    results = validate_store(store, paths)
    bad = 0
    for path, errs in results.items():
        rel = path.relative_to(store.root) if path.is_relative_to(store.root) else path
        if errs:
            bad += 1
            print(f"FAIL {rel}")
            for e in errs:
                print(f"     {e}")
        elif args.verbose:
            print(f"ok   {rel}")
    print(f"{len(results) - bad} ok, {bad} with problems")
    return 1 if bad else 0


def cmd_list(args, store: Store) -> int:
    recipes = filter_recipes(store.load_all(), category=args.category, course=args.course, tag=args.tag)
    if args.json:
        json.dump(recipes, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return 0
    if not recipes:
        print("(no recipes)")
        return 0
    width = max(len(r["slug"]) for r in recipes)
    for r in recipes:
        extras = []
        if r.get("course"):
            extras.append(f"course={r['course']}")
        if r.get("categories"):
            extras.append("[" + ", ".join(r["categories"]) + "]")
        if r.get("base_headcount"):
            extras.append(f"feeds {r['base_headcount']}")
        print(f"{r['slug']:<{width}}  {r['name']}" + (("  " + " ".join(extras)) if extras else ""))
    print(f"{len(recipes)} recipe(s)")
    return 0


def cmd_new(args, store: Store) -> int:
    slug = args.slug or slugify(args.name)
    if store.exists(slug):
        raise RecipeError(f"{store.path_for(slug)} already exists")
    r = empty_recipe(slug, args.name)
    r["course"] = args.course or ""
    r["ingredients"] = ["1 cup example ingredient (replace me)"]
    r["directions"] = ["Replace this with the first step."]
    path = store.save(r)
    print(f"Created {path}. Open it in a text editor and fill in the fields.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recipes.py",
        description="Plain-file recipe catalog for LARP feasts.",
    )
    parser.add_argument("--root", type=Path, default=None,
                        help="repo root containing recipes/ and schema/ (default: parent of tools/)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_filters(p, with_slugs=False):
        if with_slugs:
            p.add_argument("slugs", nargs="*", help="recipe slugs (default: all)")
        p.add_argument("--category", help="only recipes in this category")
        p.add_argument("--course", help="only recipes with this course")
        p.add_argument("--tag", help="only recipes with this tag")

    p = sub.add_parser("import-paprika", help="import a .paprikarecipes archive or .paprikarecipe file")
    p.add_argument("files", nargs="+")
    p.add_argument("--update", action="store_true",
                   help="replace recipes that already exist (LARP fields are kept)")
    p.add_argument("--tag", action="append", help="add this tag to every imported recipe (repeatable)")
    p.set_defaults(func=cmd_import_paprika)

    p = sub.add_parser("import-jsonld", help="import schema.org Recipe JSON-LD (bare JSON or an HTML page)")
    p.add_argument("files", nargs="+")
    p.add_argument("--update", action="store_true",
                   help="replace recipes that already exist (LARP fields are kept)")
    p.add_argument("--tag", action="append", help="add this tag to every imported recipe (repeatable)")
    p.add_argument("--source-url", help="source URL to record when the page does not state one")
    p.set_defaults(func=cmd_import_jsonld)

    p = sub.add_parser("export-paprika", help="write a .paprikarecipes archive for Paprika 3")
    add_filters(p, with_slugs=True)
    p.add_argument("-o", "--output", help="output file (default: build/recipes.paprikarecipes)")
    p.set_defaults(func=cmd_export_paprika)

    p = sub.add_parser("validate", help="check recipe files against the schema")
    p.add_argument("files", nargs="*", help="specific files (default: every recipe)")
    p.add_argument("-v", "--verbose", action="store_true", help="also list passing files")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("list", help="list recipes")
    add_filters(p)
    p.add_argument("--json", action="store_true", help="print full recipes as JSON")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("new", help="scaffold an empty recipe file")
    p.add_argument("name", help="recipe name, e.g. 'Pears in Wine Syrup'")
    p.add_argument("--slug", help="file name to use (default: derived from the name)")
    p.add_argument("--course", help="feast course, e.g. remove, side, subtlety")
    p.set_defaults(func=cmd_new)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(args.root or default_root())
    try:
        return args.func(args, store)
    except RecipeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
