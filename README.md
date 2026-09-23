# Sarkari Sahayak - citizen-scheme agent (Marathi / Hindi / English)

Describe your situation by **voice or text** -> the agent checks **8 Maharashtra + central schemes**,
shows a **live map** of what fits, lists **documents to collect**, and prints a **filled draft application (PDF)**.

**Design rule:** the LLM (optional) only *understands* your words. Eligibility, amounts and checklists come from
deterministic code + `schemes.json`, so the agent can never invent a benefit or a rule.

## Run (2 minutes)
```bash
pip install -r requirements.txt
python server.py            # open http://127.0.0.1:8000
```
Works fully **offline with no API key** (regex/keyword understanding for mr/hi/en).
For better understanding of free-form speech, set a key first (optional):
```bash
export GEMINI_API_KEY=AIza...        # Windows: set GEMINI_API_KEY=AIza...
export SAHAYAK_MODEL=gemini-2.5-flash   # optional override
```
Voice input needs Chrome or Edge (Web Speech API). Everything else works in any browser.

## MCP server (use the same tools from Claude Desktop or any MCP client)
```bash
python mcp_server.py        # stdio
```
Claude Desktop config:
```json
{"mcpServers": {"sarkari-sahayak": {"command": "python", "args": ["/full/path/to/mcp_server.py"]}}}
```
Tools: `list_schemes`, `parse_citizen_text`, `check_eligibility`, `get_document_checklist`, `generate_application_pdf`.

## Files
| file | job |
|---|---|
| `schemes.json` | the knowledge base: rules, documents, sources, verified date (3 languages) |
| `engine.py` | rules engine, text understanding, checklist, PDF, tool registry |
| `server.py` | FastAPI web app (`/api/chat`, `/api/pdf`) |
| `mcp_server.py` | exposes the same tools over MCP |
| `static/index.html` | whole UI: chat, mic, canvas scheme map, cards |
| `fonts/` | Mukta (Latin + Devanagari, OFL) so Marathi/Hindi PDFs render correctly |

## Add or fix a scheme
Edit only `schemes.json`: add an entry with `rules` (ops: eq, gte, lte, between, in, or), `docs`, `apply`, `sources`.
A rule with `"soft": true` means "worth confirming" instead of a hard no. No code change needed.

## Data honesty
Scheme data was compiled on **2026-09-19** from public pages (district/department sites where possible, otherwise
reputable explainers). Each scheme carries its source links and a confidence tag. Known soft spots:
- **Ladki Bahin**: new applications are closed (eKYC / corrections only); sources disagree on the upper age (65 vs 60).
- **MJPJAY**: sources differ on how universal the cover now is; white-card / no-card categories are flagged "confirm".
- **Sanjay Gandhi**: income limit is Rs 21,000 (higher for disabled applicants in some guidance) - treated as "confirm".
- **Shahu scholarship**: fee share (50-100%) depends on course type; window dates come from a secondary source.
Always confirm at the official portal or office before applying. This is a demo, not government advice.

## Privacy
No database, no accounts, nothing stored. Aadhaar and bank numbers are never asked - they stay blank in the PDF.
