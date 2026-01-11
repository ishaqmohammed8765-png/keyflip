# Keyflip (No Playwright) — Fanatical → Eneba Scanner

Streamlit UI (`app.py`) calls a CLI module:

`python -m keyflip.main --play` (or `--build`, `--scan`)

Outputs:
- watchlist.csv
- scans.csv
- passes.csv
- price_cache.sqlite (cache)

## Local run
pip install -r requirements.txt
streamlit run app.py
