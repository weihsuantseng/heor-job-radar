# HEOR Job Radar

Checks company career sites every morning for HEOR, outcomes research, and market access internships, and publishes the list as a website.

## How it works

- `scraper.py` reads `config.yaml`, queries each company's career site (Workday, Greenhouse, Lever, Ashby), and keeps internship titles that match the HEOR keywords.
- Results go to `docs/jobs.json`; the website (`docs/index.html`) reads that file.
- `.github/workflows/update.yml` runs everything daily on GitHub Actions, publishes the site with GitHub Pages, and opens an issue when new postings appear (GitHub emails you about new issues).

## Common tasks

- **Add a company:** edit `config.yaml` on GitHub and add an entry under `sources`. The comments in the file explain where to find each value.
- **Change keywords:** edit the `keywords` section of `config.yaml`.
- **Run now:** Actions tab > Update job list > Run workflow.
- **Check which sites work:** open the website and expand the source status section at the bottom.
