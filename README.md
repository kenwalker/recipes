# recipes

A plain-file recipe catalog for LARP feasts, kept separate from the family
Paprika collection. Every recipe is one JSON file in `recipes/`, so the whole
catalog lives in git and can be read, diffed and edited with any text editor.
No database, no server, nothing to install beyond Python 3.

```
recipes/                   one JSON file per recipe, named by slug (mujendra.json)
images/                    optional photos, referenced from a recipe's "image" field
tools/recipes.py           the command-line tool (import, export, validate, list)
schema/recipe.schema.json  what a recipe file must look like
tests/                     pytest suite
```

The two recipes currently in `recipes/` are **placeholders** written to show
the structure. Delete or replace them.

## The three commands you need

All commands are run from the repo folder. `python3` is the only requirement.

**1. Bring recipes in from Paprika.**
In Paprika 3, select recipes and choose *Export → Paprika Recipe Format*. Then:

```
python3 tools/recipes.py import-paprika ~/Downloads/Recipes.paprikarecipes
```

One file per recipe appears in `recipes/`. Recipes that already exist are
skipped; add `--update` to overwrite them with the Paprika version (your
feast-only fields such as course and headcount are kept).

**2. Check that everything is well-formed** (do this before committing):

```
python3 tools/recipes.py validate
```

It prints `FAIL` and the reason for any file that breaks the rules, for
example a missing name, a typo in a field name, or a `½` character where
`1/2` is required.

**3. Send recipes back to Paprika.**

```
python3 tools/recipes.py export-paprika --category Feast
```

This writes `build/recipes.paprikarecipes`. Open that file with Paprika (or
*Import → Paprika Recipe Format*) and it adds or updates the recipes. Give
slugs instead of a category to pick specific recipes, or nothing at all to
export the whole catalog. `-o somefile.paprikarecipes` changes the output path.

## Other commands

```
python3 tools/recipes.py list                      # everything, one line each
python3 tools/recipes.py list --course remove      # filter by course, --category or --tag
python3 tools/recipes.py new "Sops in Wine"        # scaffold recipes/sops-in-wine.json to fill in
python3 tools/recipes.py import-jsonld page.html   # import from a saved web page or a JSON-LD file
python3 tools/recipes.py <command> --help          # all options
```

`import-jsonld` reads schema.org Recipe data, which most recipe websites and
apps embed in their pages. Save the page as HTML (or paste the JSON-LD into a
`.json` file) and import it. Sectioned instructions, `@graph` wrappers and
ISO durations (`PT1H30M` becomes `1 hr 30 min`) are handled.

## Adding or editing a recipe by hand

Run `new` to get a correctly shaped file, or copy an existing one. Then open
`recipes/<slug>.json` in a text editor. The rules:

- The file name must match the `slug` field: lowercase letters, digits, hyphens.
- `ingredients` is a list of strings, one per line as you would write them.
  Write fractions as `1/2 cup`, not `½ cup`, so Paprika can scale them.
- `directions` is a list of strings, one per paragraph or step.
- `created` is a date, `YYYY-MM-DD`. `rating` is 0 to 5.
- Feast-specific fields are optional: `course` (remove, side, subtlety, sweet, ...),
  `base_headcount` (how many people the quantities feed as written),
  `allergens`, `period_notes` (historical basis), `tags`.
- Photos go in `images/` and are referenced as `"image": "images/<slug>.jpg"`.

Run `validate` afterwards. The full field list with descriptions is in
`schema/recipe.schema.json`.

## How Paprika round-tripping works

Every field Paprika exports has a matching field here, so a recipe can go
Paprika → catalog → Paprika without losing anything. Recipes imported from
Paprika keep their Paprika `uid` in `paprika_uid`, so re-exporting updates
the same recipe in Paprika instead of creating a duplicate. Recipes created
here get a uid derived from their slug, which is likewise stable across
exports. Paprika's own bookkeeping fields (favorites, trash, hashes) are
ignored on import.

## Running the tests

```
python3 -m pip install pytest
python3 -m pytest
```

## Roadmap

Not built yet, in rough order: a static HTML site exporter for plain shared
hosting, a feast planner that scales recipes to a headcount and builds a
shopping list, and possibly import from photos or scans of printed recipes.
