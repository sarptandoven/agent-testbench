# web-ui

A one-page counter checked the way a person would: `testbench run ui-check` serves `site/`, opens it in headless
Chromium, clicks "Add one" twice, takes screenshots, and checks the page says `Count: 2` with no console errors.
Needs `pip install "agent-testbench[web]"` and `playwright install chromium`.

Break `site/app.js` (call a function that does not exist) and the report names the page error.
