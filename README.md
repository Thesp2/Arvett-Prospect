# Arvett LeadFinder

Local-first business prospect research for Arvett. The first version focuses on discovering local businesses that may benefit from a new website or redesign, reviewing the evidence, and exporting an Excel workbook.

## Current MVP

- Local Flask dashboard, opened in your browser.
- Search by place name (for example, `Mehsana, Gujarat, India`).
- Business category groups: food, retail, healthcare, professional/IT, automotive, hospitality, fitness, beauty, or all.
- Optional radius filter (5–50 km); businesses without coordinates are excluded when radius is used.
- OpenStreetMap by default; Overture Maps is optional.
- Collection cap defaults to 100 (hard maximum 500); website enrichment defaults to 20 (hard maximum 100).
- Website status uses conservative labels: `NO_WEBSITE_FOUND`, `VERIFIED_NO_WEBSITE`,
  `SOCIAL_ONLY`, `WEBSITE_BROKEN`, `BASIC_WEBSITE`, `OUTDATED_WEBSITE`,
  `FUNCTIONAL_WEBSITE`, and `UNKNOWN`. A missing URL is never proof that a
  business has no website.
- Best-effort extraction of business-published emails, phones, and owner/decision-maker names from the business's own website. Fields remain blank when not found.
- XLSX workbook with `Qualified Leads`, `All Businesses`, and `Search Summary` sheets, plus CSV export.
- Overture schema inspection selects a supported category field for the active
  release and reports incompatible releases as source failures instead of
  returning a misleading empty result.
- Indian phone numbers are normalized to `+91` when the source provides a
  plausible mobile or landline number. Contact confidence and website
  opportunity scores are separate bounded values.

## Windows setup

Run these commands from the repository root in PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r gui\requirements.txt
python gui\app.py
```

Open the local URL printed by the app, normally `http://127.0.0.1:5000`. Keep the terminal window open while using it. Stop the local server with `Ctrl+C`.

## Render/Docker deployment

The Docker image installs the GUI dependencies, including Gunicorn, and starts
the Flask instance with `gui.app:app`. The container binds to
`0.0.0.0:${PORT:-5000}`, so Render's `PORT` value is honored and local Docker
runs fall back to port 5000:

```powershell
docker build -t arvett-leadfinder .
docker run --rm -p 5000:5000 -e PORT=5000 arvett-leadfinder
```

Render should use the repository `Dockerfile` without replacing its startup
command. No API keys or secrets are required in the image.

## Recommended first search

- Business category: All local businesses or one targeted category.
- Location: `Mehsana, Gujarat, India`.
- Source: OpenStreetMap only.
- Radius: selected area boundary for the first run; try 5–10 km for a tighter local search.
- Maximum businesses: 50–100.
- Website checks: enabled, cap 10–20 for the first run.

## Important limitations

- OpenStreetMap coverage is community-maintained and varies by locality. Missing phone/email/website tags do not prove that those details do not exist.
- “No website found” means no website URL was present in the collected source record; it is not proof the business has no website.
- `WEBSITE_BROKEN` means the configured check observed an HTTP, SSL, timeout, or
  other request failure; review the stored evidence before contacting a lead.
- Owner/decision-maker discovery is best-effort and only uses publicly published information found on the business website. Do not treat inferred or missing values as verified.
- Employee counts and company size are left `Unknown` unless a reliable source is available; the tool does not invent company size.
- Website audit failures are marked for rechecking rather than treated as confirmed broken sites.
- Use data sources in line with their licenses and terms. The workbook records the source record URL to support manual review.
- Running `python gui\app.py` directly uses Flask's development server and is
  intended for local use. Container deployments use Gunicorn instead.

## Outputs

Generated files are written under `gui/_output/`. The Excel workbook is the main review file; the CSV is a simple CRM-friendly export.

## Development checks

```powershell
python -m compileall gui leadgen
python -m unittest leadgen.tests.test_arvett_mvp -v
```
