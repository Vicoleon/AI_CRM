"""Cubie for Chrome — the browser extension's server side.

``connections`` mints and checks the per-browser key. The new tab's payload is
built in ``backend/services/browser_glance.py`` (it reads the priorities card
through the home router's read path) and served by
``backend/routers/browser_extension.py``. The extension itself lives in
``chrome_extension/`` at the repo root; ``docs/chrome_extension/README.md`` is
the operator's guide.
"""
