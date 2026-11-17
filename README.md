# PrintBench

PrintBench is a local library for 3D projects: immutable revisions, recorded check evidence, rotatable STL previews, and hash-verified downloads. It does not generate models, run slicers, or control printers. This checkout includes source, tests, a synthetic example, and an optional Hermes skill, not a populated library or a Hermes plugin.

## Run locally

Requires Python 3.11+ on Linux. No package install or frontend build. Run from the checkout root:

```sh
library="$HOME/3d-projects/printbench-data"
python3 -m printbench --data "$library" serve --port 8765
```

`--data` is a global option and goes before the command. It names the library directory, not this checkout or an artifact source. If omitted, it defaults to the path shown above. Prefer an explicit path, especially when writing. Use an owned directory with no symlink components. Listing a missing library returns an empty list; a successful first publish creates it. Serving an empty or missing library does not seed it.

Open the printed URL on the machine running PrintBench. HTTP binds only to `127.0.0.1` and is read-only, but is **not authenticated**: other local processes can read the artifacts and manifests, including recorded source paths. Do not expose this service through a LAN listener, public proxy, or port forward to untrusted users. If the port is busy, choose another port or use `--port 0` and read the assigned URL; do not stop an unrelated listener. Press Ctrl+C to stop the server you started.

## Try a synthetic project

Run from the checkout root. This uses a new disposable library, not your default library:

```sh
demo=$(mktemp -d) || exit 1
mkdir "$demo/source"
printf '%s\n' 'Synthetic PrintBench download example.' > "$demo/source/sample.txt"
python3 -m printbench --data "$demo/library" publish \
  --spec examples/sample.json --source "$demo/source"
python3 -m printbench --data "$demo/library" get sample --revision r1
python3 -m printbench --data "$demo/library" list
python3 -m printbench --data "$demo/library" serve --port 0
```

The example has no model or test pass claims. After stopping that server, you can remove the disposable directory you just created. To import your own project, adapt the JSON spec: `id`, `revision`, `name`, `requirements`, `dimensions_mm` (positive finite X/Y/Z), `notes`, and `artifacts` are required. `thumbnail` may name a supplied PNG/JPEG. `status` records `mesh`, `slicer`, and `physical` independently; each defaults to `unknown`. `passed` and `failed` require a matching `status_evidence` entry naming a supplied artifact. This is recorded evidence, not validation by PrintBench.

Artifacts must be regular files with safe basenames (letters, digits, dots, underscores, or hyphens; start with a letter or digit). `manifest.json` is reserved. Limits: 64 MiB/file, 256 MiB/publish, and 1 MiB per input spec, serialized manifest, or project index. Input files are copied, never executed or moved. The browser preview reads binary or ASCII STL only (20 MiB / 50,000 triangles); it interprets coordinates as millimetres, not measured dimensions. Other supplied artifact types can be downloaded.

CLI successes are JSON on stdout; validation/filesystem failures are JSON on stderr with a nonzero exit. New content needs a new revision ID. If a write fails or is interrupted, check the exact revision with `get` before retrying; the commit may already be visible. Do not edit manifests or turn an existing revision into a different one.

## Back up and restore

The library's `project.json` indexes, `revisions/` directories, manifests, and artifacts belong together. Back up the **whole library**, separately from this source checkout. Stop or otherwise quiesce all writers before copying. Use a new owned path on trusted backup storage; for example, after setting `backup_dir` to an unused absolute directory path:

```sh
mkdir -m 700 -- "$backup_dir" && cp -a -- "$library" "$backup_dir/library"
```

To verify or restore, copy the backup to a separate new directory (never on top of an active library), then use `--data` to inspect it before switching readers or writers:

```sh
mkdir -m 700 -- "$restore_dir" && cp -a -- "$backup_dir/library" "$restore_dir/library"
python3 -m printbench --data "$restore_dir/library" list
python3 -m printbench --data "$restore_dir/library" get YOUR_PROJECT --revision YOUR_REVISION
```

Set `restore_dir` to an unused absolute path first. Compare the restored project/revision list and verify important downloads against their manifest hashes; `list` alone does not verify artifact bytes. Stop and inspect if copying or verification fails. Revisions are immutable through PrintBench, not protected from OS-level edits. Unindexed remnants after a crash are not published; do not remove them without inspecting and backing up the library.

## Optional Hermes skill

The standalone app does not need Hermes. To install the [bundled skill](skills/printbench/SKILL.md), use Hermes in the **intended profile**. Check for an existing `printbench` skill (including disabled skills) with `hermes -p YOUR_PROFILE skills list`; do not overwrite an existing skill. Ask its agent to read the bundled `SKILL.md` from this checkout and call native `skill_manage` with a `create` operation using that exact content. Read it back with `skill_view(name="printbench")`. If the profile doesn't expose those tools, stop rather than editing another profile or bypassing its controls. Give the agent the absolute checkout and library paths explicitly; skill installation does not start the server or create a library.

## Tests

```sh
python3 -m unittest discover -s tests -v
node --test tests/viewer.test.mjs
node --check web/app.js
python3 -m compileall -q printbench
```

Node.js 22 is sufficient for these checks; it is not a runtime dependency. Tests use temporary files and ephemeral loopback ports, not the default library.
