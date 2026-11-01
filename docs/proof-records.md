# Proof records

<!-- From README.md, "Proof records", and docs/reference.md, "Proof records" and "Run folder". Edit them there.
The screenshots are made by docs/assets/make_screenshots.py from the runs of docs/tour.md. -->
--8<-- "README.md#proof-records"

`testbench ui` for the demo project after the runs of [the tour](tour.md) (a screenshot):

[![The testbench ui index for the tiny-model project: 6 runs, 5 passed, $0.000; a plans table where smoke's latest run passed, 83% of its 6 runs passed, with a trend line for accuracy that dips once and ends at 0.98, and a flat one for epochs; three plans never run; a runs table, newest first, each with its plan, where it ran, minutes, cost and commit, the one FAIL marked as run on uncommitted changes; and the loop: stopped, best accuracy 0.98, 2 tries, 1 kept.](assets/ui-index.png)](assets/ui-index.png)

## What a record holds

--8<-- "docs/reference.md#proof-records"

The end of a run's page, `report.html` in its folder, with its list of evidence files opened (a screenshot of the
loop's best run in the tour):

[![The end of a run's page: the 11 evidence files with their sizes and sha256 (logs, result.json, the executed notebook, the plots, metrics.json), then the provenance - the commit on its branch, not dirty, no untracked files; Python 3.13.17 CPython on Linux x86_64, the interpreter's path, 182 packages and their sha256; and the sha256 over all the evidence.](assets/ui-run.png)](assets/ui-run.png)

## The run folder

--8<-- "docs/reference.md#run-folder"
