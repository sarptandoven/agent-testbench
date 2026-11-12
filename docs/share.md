# Share a run

A run's proof lives in `.testbench/` on your machine. `testbench share` turns one run into a static page anyone
can open - someone who has never heard of testbench included - and check: the report with its plots and tear sheet,
the evidence files it links, and a manifest of their sha256, so `testbench verify --from URL` says whether anything
changed since.

```bash
testbench share --to ./public --dry-run       # every file it would publish, its size and why; publishes nothing
testbench share --to git:gh-pages/runs        # commit it to the gh-pages branch (you push it)
testbench verify --from https://you.github.io/proj/runs/20261112-094032-smoke/
testbench badge smoke                         # a README badge: "testbench | 14 checks passed"
```

## What it publishes

The run goes in a folder of its own, `DEST/RUN/`:

| file | what it is |
|---|---|
| `index.html` | the run's report as one page: the verdict, what failed and why, every check and what it found, the plots (linked, not inlined), the tear sheet of a trading step, the list of evidence files with their sha256, and where it ran |
| `report.json` | the report as data, with the sha256 the run recorded for every evidence file and the one over all of them (`evidence_sha256`) |
| `badge.svg` | the run's badge, shown at the top of the page |
| `steps/...` | the evidence files the page links: images, logs, results, the files the steps kept with `artifacts:` (the CSV or JSON a check read is said so), byte for byte |
| `testbench-manifest.json` | the sha256 and size of every file above, the run's `evidence_sha256`, what was left out and why, and what was scrubbed |

Before anything goes out it lists every file with its size and why it is there, the total, what it leaves out and
why, and what it scrubbed (from a real run):

```
$ testbench share --to ./public --dry-run
Run 20261112-094032-p (plan p: 4 checks passed) to public/20261112-094032-p/ (a folder on this machine):

  index.html                                6.3 KB  the report as one page: what ran, the checks and what they found, the evidence
  report.json                               8.3 KB  the report as data, with the sha256 the run recorded for each evidence file (scrubbed)
  badge.svg                                  579 B  a README badge: testbench: 4 checks passed
  steps/01-nb-main/artifacts/out/m.json       36 B  a file the step kept, which its checks read
  steps/01-nb-main/cell02_image1.png          70 B  an image the page shows
  steps/01-nb-main/upload_queue.json           2 B  the files the notebook was given to upload
  steps/02-cmd/log.txt                        16 B  the step's output
  steps/02-cmd/result.json                   287 B  the step's result as testbench recorded it
  testbench-manifest.json                   2.9 KB  the sha256 of every file here, for `testbench verify --from`

9 files, 18.4 KB in all; 5 of the run's 8 evidence files.

Left out:
  steps/01-nb-main/executed.ipynb           2.2 KB  holds the project folder (--keep-paths publishes it as it is)
  steps/01-nb-main/log.txt                   165 B  holds the project folder (--keep-paths publishes it as it is)
  steps/01-nb-main/result.json              1.4 KB  holds the project folder (--keep-paths publishes it as it is)
An evidence file is published byte for byte or not at all (its sha256 is the proof), so one that holds this machine's paths is left out.

Never published: state.json, plan.json, the ledgers, limits, NOTES.md, .env files and keys, or any file holding a secret's value, a step's env: value or a token in this environment.

Scrubbed from index.html and report.json:
  the project folder /home/ada/proj -> <project> (3 times)

Nothing was published (--dry-run). To publish it, a person runs: testbench share 20261112-094032-p --to ./public
```

## What it never publishes

- **testbench's own state**: `state.json`, `plan.json` (it holds the steps' `env:` values), the ledgers, the limits,
  `NOTES.md`, logs of its own. Only the report and the step folders' evidence are ever looked at.
- **Secrets and keys by name**: `.env` and `.env.*`, `.netrc`, `.modal.toml`, `id_rsa`/`id_ed25519`, `*.pem`,
  `*.key`, `credentials.json` and anything in a `.git`, `.ssh` or `.aws` folder, even when an `artifacts:` glob
  caught one.
- **Secret values**: any evidence file that holds the value of a secret named under `secrets:` (a step's or
  `sessions:`), of a step's `env:`, or of an environment variable named like a key, token or password
  (`*_KEY`, `*_TOKEN`, `*SECRET*`, `*PASSWORD*`, `*WEBHOOK*`) is left out, and in the page and report.json the value
  becomes `<NAME>`. A value is named in the listing, never shown. When a step names a secret that is not set where
  you share (a Modal secret, say), its value cannot be looked for: that step's text evidence is left out and what it
  printed is kept off the page. Set it in this shell and share again to include them.
- **This machine's paths**: the page and report.json are scrubbed of the project folder (`<project>`), your home
  folder (`~`) and your user name in another home (`/home/<user>`). An evidence file cannot be scrubbed - its sha256
  is the proof - so one that holds such a path is left out; `--keep-paths` publishes it as it is.
- Files over 25 MB (`--max-file-mb N`), and files whose names a web server would read differently.

A run whose evidence no longer matches its report (`testbench verify` fails) is not shared at all: it proves nothing.

## Where it goes

`--to` takes three kinds of place, and the run always goes in `DEST/RUN/`, so one place holds many runs:

| `--to` | what happens |
|---|---|
| `./public` (any folder) | the files are copied there. A folder of an earlier share of the same run is replaced; a folder with other files is never touched. Put it on any static host - GitHub Pages, Netlify, an S3 bucket - it is plain files |
| `git:gh-pages/runs` | a commit on branch `gh-pages` (created if it is not there, with a `.nojekyll`), the run in `runs/RUN/`, in the project's git repository. It is made with git plumbing and a temporary index - your working tree, your index and your current branch are never touched - and refused for a branch checked out anywhere. **It is never pushed**: testbench prints the `git push origin gh-pages` for you, and the GitHub Pages address the page will have. The commit message carries the run's `evidence_sha256` |
| `s3://bucket/prefix` | `aws s3 cp --recursive` to `s3://bucket/prefix/RUN/`, with the AWS CLI as it is signed in: testbench never reads or passes credentials. Any S3-compatible service the CLI reaches works (`AWS_ENDPOINT_URL`). The page is public only if the bucket allows public reads |

## A person says yes

Publishing is outward-facing, so it is treated like an approval: without `--dry-run` it lists the files and asks
`Publish these files to ...? [y/N]` in a terminal, and refuses (exit 2) anywhere it cannot ask - a script, a pipe,
an agent's shell - unless `--yes` says a person read the list and agreed. In Claude Code the guard hook asks you
before `testbench share` runs at all (it is on `guard.ask` by default, and asked about even where a project's own
`guard.ask` leaves it out); `--dry-run` needs no question. An agent shows you the dry run and waits for your yes.

## Checking a shared run: verify --from

```bash
pip install agent-testbench
testbench verify --from https://ada.github.io/proj/runs/20261112-094032-p/     # or the folder, after a download
```

No project and no testbench.yaml are needed. It reads `testbench-manifest.json` and checks:

1. every file it lists is there, with its size and sha256 (and, for a folder, that no file was added beside them);
2. report.json's list of evidence hashes adds up to its `evidence_sha256`, and the manifest names the same one;
3. every published evidence file has the sha256 the run recorded when it ended.

Exit 0 when all of it holds, 1 with each file that does not (`changed: steps/01-nb-main/artifacts/out/m.json (its
sha256 is not the one the run recorded)`), 2 when it could not be checked (no manifest there, the address is
unreachable). A file edited after publishing fails the first check; edited along with the manifest, it fails the
third.

What it cannot tell: someone who rewrote report.json and the evidence together, and made a new manifest, publishes a
bundle that agrees with itself. So it prints the run's `evidence_sha256`; compare it with the one the run printed
when it ended - its report.md, the CI log, or the commit message on the `gh-pages` branch. A run made by the
[GitHub Action](github-action.md) with `attest: true` is also signed; `testbench verify --attestation` checks that
in the project ([Proof records](proof-records.md)).

## A README badge

```bash
$ testbench badge smoke
wrote /home/ada/proj/testbench-smoke.svg: "testbench: 14 checks passed" (from run 20261112-094032-smoke)
For README.md in /home/ada/proj:
  ![testbench: 14 checks passed](testbench-smoke.svg)
Run it again after each run to keep it current; `testbench share` puts one next to the page it publishes.
```

The badge is drawn by testbench (no service, nothing fetched): `14 checks passed` on green, `2 of 14 checks failed`
on red, `cancelled` on amber, `no runs` on grey. The words carry the result, so it reads without the colours, and
white text is 4.5:1 or more on each. Without a plan it is the latest finished run's plan; `-o` writes it elsewhere
and `--link URL` makes the markdown a link (to the page `testbench share` printed, say). `testbench share` puts the
run's badge next to its page, `badge.svg`, and prints the markdown for it.

## Readable by everyone

The shared page, like the HTML report, `testbench ui` and the dashboard, is built to be read without sight and
without a mouse: one heading outline, table headers with their scope, every evidence image's alt text naming its
step and file, a visible focus ring, text at 4.5:1 or more against its background in light and dark, and every
result in words (PASS, FAIL, ok, missed) as well as colour. `tests/test_accessibility.py` checks each page.
