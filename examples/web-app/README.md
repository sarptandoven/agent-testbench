# web-app

"Split the bill": a page with a form (`static/`) and a small JSON API that does the sums (`app.py`, standard
library only; `python app.py 8974` serves it).

The `ui` plan starts the app, opens it in headless Chromium and uses it like a person: it fills in a total of $50, 3
people and a 15% tip, submits, and checks the page says `Each person pays $19.17`; then it submits a bill for no
one and checks the page explains what is wrong. Both steps check the title and that nothing went to the console as
an error (an uncaught exception, a failed request, a `console.error`), and take screenshots before and after.

```bash
pip install "agent-testbench[web]" && playwright install chromium
testbench demo web-app          # or, in this folder: testbench run ui
```

The report lists each check and the screenshots, which are in the run's folder (`testbench report --format html`
shows them inline). Break the app - round down in `split()`, or rename `#result` in `static/index.html` - and the
report says which text the page no longer shows, or which page error it threw.
