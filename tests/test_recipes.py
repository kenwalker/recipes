"""Tests for tools/recipes.py. Run with:  python3 -m pytest"""

import gzip
import importlib.util
import io
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "recipes.py"

_spec = importlib.util.spec_from_file_location("recipes_cli", TOOL_PATH)
cli = importlib.util.module_from_spec(_spec)
sys.modules["recipes_cli"] = cli
_spec.loader.exec_module(cli)


@pytest.fixture
def root(tmp_path):
    """An empty catalog root with the real schema copied in."""
    (tmp_path / "schema").mkdir()
    shutil.copy(REPO_ROOT / "schema" / "recipe.schema.json", tmp_path / "schema")
    (tmp_path / "recipes").mkdir()
    return tmp_path


def run(root, *argv):
    return cli.main(["--root", str(root), *argv])


def read_archive(path):
    out = {}
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            out[name] = json.loads(gzip.decompress(zf.read(name)).decode("utf-8"))
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_slugify():
    assert cli.slugify("Pears in Wine Syrup") == "pears-in-wine-syrup"
    assert cli.slugify("  Crème Brûlée! ") == "creme-brulee"
    assert cli.slugify("***") == "recipe"


def test_plain_fractions():
    assert cli.plain_fractions("½ cup") == "1/2 cup"
    assert cli.plain_fractions("1½ tsp") == "1 1/2 tsp"
    assert cli.plain_fractions("2 ⅔ cups") == "2 2/3 cups"
    assert cli.plain_fractions("1⁄4 lb") == "1/4 lb"
    assert cli.plain_fractions("1/2 cup") == "1/2 cup"


def test_human_duration():
    assert cli.human_duration("PT1H30M") == "1 hr 30 min"
    assert cli.human_duration("PT45M") == "45 min"
    assert cli.human_duration("PT2H") == "2 hrs"
    assert cli.human_duration("P1DT2H") == "26 hrs"
    assert cli.human_duration("PT0M") == ""
    assert cli.human_duration("about an hour") == "about an hour"
    assert cli.human_duration(None) == ""


# ---------------------------------------------------------------------------
# Paprika round trip
# ---------------------------------------------------------------------------

PAPRIKA_RECIPE = {
    "uid": "0F3A2C4E-1111-4222-8333-444455556666",
    "name": "Mujendra",
    "ingredients": "1 cup brown lentils\n1 cup rice\n2 onions, sliced\n½ cup olive oil\n1 tsp cumin\nsalt",
    "directions": "Simmer the lentils until almost tender.\n\nFry the onions until deep brown.\n\nAdd rice and cook until done.",
    "description": "Lentils and rice.",
    "notes": "Doubles well.",
    "servings": "6",
    "prep_time": "10 min",
    "cook_time": "45 min",
    "total_time": "55 min",
    "source": "Anonymous Andalusian Cookbook",
    "source_url": "https://example.org/mujendra",
    "categories": ["Feast", "Side"],
    "rating": 4,
    "image_url": None,
    "photo": None,
    "photo_hash": None,
    "photo_data": None,
    "created": "2024-03-01 18:22:05",
    # Unknown / app-state fields that the importer must tolerate:
    "hash": "abc123",
    "scale": None,
    "on_favorites": True,
    "in_trash": False,
    "photos": [],
}


def make_archive(path, recipes):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zf:
        for r in recipes:
            zf.writestr(f"{r['name']}.paprikarecipe",
                        gzip.compress(json.dumps(r).encode("utf-8")))
    return path


def test_paprika_import_then_export_round_trips(root, tmp_path, capsys):
    archive = make_archive(tmp_path / "in.paprikarecipes", [PAPRIKA_RECIPE])

    assert run(root, "import-paprika", str(archive)) == 0
    path = root / "recipes" / "mujendra.json"
    assert path.exists()
    r = json.loads(path.read_text())

    assert r["slug"] == "mujendra"
    assert r["name"] == "Mujendra"
    assert r["ingredients"] == ["1 cup brown lentils", "1 cup rice", "2 onions, sliced",
                                "1/2 cup olive oil", "1 tsp cumin", "salt"]
    assert r["directions"] == ["Simmer the lentils until almost tender.",
                               "Fry the onions until deep brown.",
                               "Add rice and cook until done."]
    assert r["categories"] == ["Feast", "Side"]
    assert r["rating"] == 4
    assert r["created"] == "2024-03-01"
    assert r["paprika_uid"] == PAPRIKA_RECIPE["uid"]
    assert "hash" not in r and "on_favorites" not in r
    assert list(r)[:2] == ["slug", "name"]  # canonical key order

    # Validate passes on the imported file.
    assert run(root, "validate") == 0

    # Export and compare against the original Paprika object.
    out = tmp_path / "out.paprikarecipes"
    assert run(root, "export-paprika", "mujendra", "-o", str(out)) == 0
    entries = read_archive(out)
    assert list(entries) == ["mujendra.paprikarecipe"]
    p = entries["mujendra.paprikarecipe"]

    assert p["uid"] == PAPRIKA_RECIPE["uid"]
    assert p["name"] == PAPRIKA_RECIPE["name"]
    assert p["ingredients"] == PAPRIKA_RECIPE["ingredients"].replace("½", "1/2")
    assert p["directions"] == PAPRIKA_RECIPE["directions"]
    for f in ("description", "notes", "servings", "prep_time", "cook_time", "total_time",
              "source", "source_url", "categories", "rating", "image_url",
              "photo", "photo_hash", "photo_data"):
        assert p[f] == PAPRIKA_RECIPE[f], f
    assert p["created"] == "2024-03-01 00:00:00"

    # Importing the export again is a no-op without --update.
    assert run(root, "import-paprika", str(out)) == 0
    assert "skip" in capsys.readouterr().out
    assert sorted(x.name for x in (root / "recipes").iterdir()) == ["mujendra.json"]


def test_paprika_multi_recipe_archive_and_bare_file(root, tmp_path):
    second = dict(PAPRIKA_RECIPE, uid="AAAAAAAA-0000-4000-8000-000000000001",
                  name="Pears in Syrup", categories=["Sweet"])
    archive = make_archive(tmp_path / "two.paprikarecipes", [PAPRIKA_RECIPE, second])
    assert run(root, "import-paprika", str(archive)) == 0
    assert (root / "recipes" / "mujendra.json").exists()
    assert (root / "recipes" / "pears-in-syrup.json").exists()

    # A bare .paprikarecipe (gzip JSON, not zipped) also imports.
    third = dict(PAPRIKA_RECIPE, uid="AAAAAAAA-0000-4000-8000-000000000002", name="Frumenty")
    bare = tmp_path / "frumenty.paprikarecipe"
    bare.write_bytes(gzip.compress(json.dumps(third).encode("utf-8")))
    assert run(root, "import-paprika", str(bare)) == 0
    assert (root / "recipes" / "frumenty.json").exists()


def test_paprika_update_keeps_larp_fields_and_slug(root, tmp_path):
    archive = make_archive(tmp_path / "in.paprikarecipes", [PAPRIKA_RECIPE])
    run(root, "import-paprika", str(archive))
    path = root / "recipes" / "mujendra.json"
    r = json.loads(path.read_text())
    r.update({"course": "side", "base_headcount": 12, "allergens": ["none"],
              "period_notes": "13th c. al-Andalus", "tags": ["vegan"]})
    path.write_text(json.dumps(r))

    # Same uid, new name and ingredients, as if edited in Paprika.
    changed = dict(PAPRIKA_RECIPE, name="Mujendra (lentils and rice)", ingredients="2 cups lentils")
    archive2 = make_archive(tmp_path / "in2.paprikarecipes", [changed])
    run(root, "import-paprika", str(archive2), "--update")

    files = sorted(x.name for x in (root / "recipes").iterdir())
    assert files == ["mujendra.json"]          # matched by uid, slug unchanged
    r2 = json.loads(path.read_text())
    assert r2["name"] == "Mujendra (lentils and rice)"
    assert r2["ingredients"] == ["2 cups lentils"]
    assert r2["course"] == "side"
    assert r2["base_headcount"] == 12
    assert r2["period_notes"] == "13th c. al-Andalus"
    assert r2["tags"] == ["vegan"]


def test_paprika_photo_round_trip(root, tmp_path):
    import base64
    jpeg = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
    with_photo = dict(PAPRIKA_RECIPE, photo="abc.jpg", photo_hash="x",
                      photo_data=base64.b64encode(jpeg).decode())
    run(root, "import-paprika", str(make_archive(tmp_path / "p.paprikarecipes", [with_photo])))
    r = json.loads((root / "recipes" / "mujendra.json").read_text())
    assert r["image"] == "images/mujendra.jpg"
    assert (root / "images" / "mujendra.jpg").read_bytes() == jpeg

    out = tmp_path / "out.paprikarecipes"
    run(root, "export-paprika", "-o", str(out))
    p = read_archive(out)["mujendra.paprikarecipe"]
    assert base64.b64decode(p["photo_data"]) == jpeg
    assert p["photo"] == "mujendra.jpg"
    assert p["photo_hash"]


def test_export_category_filter_and_stable_uid(root, tmp_path):
    for slug, cats in (("a", ["Feast"]), ("b", ["Sweet"]), ("c", ["feast", "Sweet"])):
        r = cli.empty_recipe(slug, slug.upper())
        r["categories"] = cats
        r["ingredients"], r["directions"] = ["x"], ["y"]
        cli.Store(root).save(r)
    out = tmp_path / "feast.paprikarecipes"
    assert run(root, "export-paprika", "--category", "feast", "-o", str(out)) == 0
    entries = read_archive(out)
    assert sorted(entries) == ["a.paprikarecipe", "c.paprikarecipe"]
    # A recipe with no paprika_uid gets the same uid every export.
    uid1 = entries["a.paprikarecipe"]["uid"]
    run(root, "export-paprika", "a", "-o", str(out))
    assert read_archive(out)["a.paprikarecipe"]["uid"] == uid1
    assert uid1 == uid1.upper() and len(uid1) == 36

    assert run(root, "export-paprika", "nope", "-o", str(out)) == 2


# ---------------------------------------------------------------------------
# JSON-LD import
# ---------------------------------------------------------------------------

BARE_JSONLD = {
    "@context": "https://schema.org",
    "@type": "Recipe",
    "name": "Pears in Wine Syrup",
    "description": "Poached pears, <em>sweet</em> &amp; spiced.",
    "author": {"@type": "Person", "name": "Ken"},
    "url": "https://example.org/pears",
    "image": ["https://example.org/pears.jpg"],
    "recipeYield": ["8", "8 servings"],
    "prepTime": "PT15M",
    "cookTime": "PT1H",
    "totalTime": "PT1H15M",
    "recipeCategory": "Dessert",
    "recipeCuisine": ["Medieval", "English"],
    "keywords": "feast, sweet, pears",
    "recipeIngredient": ["8 firm pears", "2 cups red wine", "½ cup honey", "1 tsp ginger"],
    "recipeInstructions": [
        {"@type": "HowToSection", "name": "Pears",
         "itemListElement": [
             {"@type": "HowToStep", "text": "Peel the pears, leaving stems on."},
             {"@type": "HowToStep", "text": "Simmer in wine until tender."},
         ]},
        {"@type": "HowToSection", "name": "Syrup",
         "itemListElement": [
             {"@type": "HowToStep", "text": "Reduce the wine with honey and ginger by half."},
         ]},
    ],
    "nutrition": {"@type": "NutritionInformation", "calories": "210 kcal"},
}


def test_jsonld_bare_object_with_sections(root, tmp_path):
    f = tmp_path / "pears.json"
    f.write_text(json.dumps(BARE_JSONLD))
    assert run(root, "import-jsonld", str(f)) == 0
    r = json.loads((root / "recipes" / "pears-in-wine-syrup.json").read_text())

    assert r["name"] == "Pears in Wine Syrup"
    assert r["description"] == "Poached pears, sweet & spiced."
    assert r["ingredients"] == ["8 firm pears", "2 cups red wine", "1/2 cup honey", "1 tsp ginger"]
    assert r["directions"] == [
        "Pears:", "Peel the pears, leaving stems on.", "Simmer in wine until tender.",
        "Syrup:", "Reduce the wine with honey and ginger by half.",
    ]
    assert r["servings"] == "8 servings"
    assert r["prep_time"] == "15 min"
    assert r["cook_time"] == "1 hr"
    assert r["total_time"] == "1 hr 15 min"
    assert r["categories"] == ["Dessert", "Medieval", "English"]
    assert r["source"] == "Ken"
    assert r["source_url"] == "https://example.org/pears"
    assert r["image_url"] == "https://example.org/pears.jpg"
    assert r["tags"] == ["feast", "sweet", "pears"]
    assert r["nutritional_info"] == "Calories: 210 kcal"
    assert "paprika_uid" not in r
    assert run(root, "validate") == 0


HTML_PAGE = """<!doctype html>
<html><head><title>Frumenty</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
  {"@type":"WebSite","name":"Feast Blog"},
  {"@type":"Recipe","name":"Frumenty","recipeYield":6,
   "recipeIngredient":["1 cup cracked wheat","3 cups milk","2 egg yolks","pinch saffron"],
   "recipeInstructions":"Soak the wheat overnight.\\nSimmer in milk until thick.<br>Beat in the yolks and saffron.",
   "totalTime":"PT2H30M","mainEntityOfPage":{"@id":"https://example.org/frumenty"}}
]}
</script>
<script type="application/ld+json">{"@type":"BreadcrumbList"}</script>
</head><body><h1>Frumenty</h1></body></html>
"""


def test_jsonld_html_page_with_graph_and_string_instructions(root, tmp_path):
    f = tmp_path / "frumenty.html"
    f.write_text(HTML_PAGE)
    assert run(root, "import-jsonld", str(f)) == 0
    r = json.loads((root / "recipes" / "frumenty.json").read_text())
    assert r["servings"] == "6"
    assert r["directions"] == ["Soak the wheat overnight.", "Simmer in milk until thick.",
                               "Beat in the yolks and saffron."]
    assert r["total_time"] == "2 hrs 30 min"
    assert r["source_url"] == "https://example.org/frumenty"
    assert len(r["ingredients"]) == 4


def test_jsonld_plain_step_list():
    node = {"@type": "Recipe", "name": "X",
            "recipeInstructions": [{"@type": "HowToStep", "text": "One."}, "Two.", {"text": "Three."}]}
    assert cli.jsonld_to_recipe(node)["directions"] == ["One.", "Two.", "Three."]


# ---------------------------------------------------------------------------
# validate / list / new
# ---------------------------------------------------------------------------

def test_validate_reports_problems(root, capsys):
    bad = {"slug": "wrong-slug", "name": "", "ingredients": ["½ cup"], "directions": "not a list",
           "rating": 9, "created": "yesterday", "bogus": 1}
    (root / "recipes" / "bad.json").write_text(json.dumps(bad))
    assert run(root, "validate") == 1
    out = capsys.readouterr().out
    for needle in ("FAIL recipes/bad.json", "does not match file name", "must not be empty",
                   "plain fractions", "expected array", "above maximum", "not a valid date",
                   "unknown field 'bogus'"):
        assert needle in out, needle


def test_new_then_list(root, capsys):
    assert run(root, "new", "Sops in Wine", "--course", "remove") == 0
    assert (root / "recipes" / "sops-in-wine.json").exists()
    assert run(root, "validate") == 0
    assert run(root, "new", "Sops in Wine") == 2  # already exists
    run(root, "list", "--course", "remove")
    out = capsys.readouterr().out
    assert "sops-in-wine" in out and "course=remove" in out
    run(root, "list", "--course", "subtlety")
    assert "(no recipes)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The checked-in seed recipes must be valid.
# ---------------------------------------------------------------------------

def test_seed_recipes_validate():
    results = cli.validate_store(cli.Store(REPO_ROOT))
    assert results, "no seed recipes found"
    for path, errs in results.items():
        assert not errs, f"{path}: {errs}"
