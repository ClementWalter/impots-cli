---
name: impots-cli
description: Read and act on the user's impots.gouv.fr "espace particulier" from the terminal via the bundled `impots` command — list and bulk-download every document of the Documents page (avis d'impôt, déclarations, taxes foncières/habitation, relances, all years) as PDF, read the messagerie sécurisée (threads, attachments, accusés de réception), and file a new demande with attachments (dry run unless --yes). Drives a dedicated Chrome over CDP because the DGFiP login sits behind a Radware bot manager. Use when the user asks for their tax documents, avis d'impôt, to read or answer the DGFiP, or to check for new messages. Triggers - "impots.gouv", "télécharge mes avis d'impôt", "documents impots", "messagerie impots", "écris aux impôts", "message DGFiP", "SIP", "avis de taxe foncière".
---

# impots-cli

## How to invoke

`impots` is on `$PATH` (linked by `uv run ~/Documents/claudine/scripts/link_clis.py`).
Fallback: `<skill-dir>/bin/impots`. Each command takes `--help` with examples.

## Authentication

No credential is stored. The CLI drives a dedicated Chrome (profile
`~/.impots-chrome`, DevTools port 9224, started on demand). That profile keeps the
session cookies and the trusted-device state between runs.

- Not signed in → every command fails with
  ``Not signed in to impots.gouv.fr — run `impots login` …``.
- `impots login` opens the window on the login page and waits (default 300 s) until
  the person signs in (numéro fiscal + mot de passe, or FranceConnect). The person
  types the password; an agent never does.
- The DGFiP session expires quickly when idle (observed: gone within the same
  working session, exact duration not measured), so expect to ask the user for
  `! impots login` before most runs.

## Output conventions

- Default: one line per record, the id needed by the next command last, after a TAB.
- `--json`: normalized JSON (ISO dates, stable keys). Progress and warnings go to stderr.
- Files go to `~/Downloads/impots/` by default; paths printed are absolute.

## Commands

```bash
# Read
impots whoami                              # name, numéro fiscal, unread message count
impots documents                           # every document, all years (id last)
impots documents --year 2026 --json
impots documents --match "taxe fonci"      # regex on label/description, client-side
impots messages                            # demandes (threads), latest first (id last)
impots messages --unread
impots message 1234567890                  # full thread, oldest first; marks it read
impots message 1234567890 1234567891 --json
impots topics                              # subject codes for a new demande

# Write
impots download --all                      # → ~/Downloads/impots/<year>/<Libellé>.pdf, skips existing
impots download --year 2025 -o ~/Taxes
impots download 0123456789 abcdef0123      # by id from `impots documents`
impots message 1234567890 --save           # + attachments and accusés de réception
impots send --topic 14 --body-file q.txt --attach avis.pdf          # DRY RUN: validated recap, then abandoned
impots send --topic 14 --body-file q.txt --attach avis.pdf --yes    # really sends
impots login                               # terminal only: wait for a manual sign-in
```

## Recipes

- **Archive every tax document**: `impots download --all -o "<drive>/Impots"` — one
  folder per year; rerunning only fetches new documents.
- **Anything new from the DGFiP?** `impots whoami --json` (`unread_messages`) then
  `impots messages --unread` and `impots message <id>`.
- **Write to the tax office**: pick a code with `impots topics`, draft the text in a
  file, run `impots send … ` without `--yes`, show the recap to the user, and add
  `--yes` only after they approve it. `sent<TAB><id>` is printed only once the new
  demande appears in « Mes échanges »; otherwise it exits 1 with "send NOT confirmed".
  Never resend blindly on that error: have the user check the website first, since
  the demande may have gone through anyway.
- **Get the latest avis d'impôt**: `impots documents --match "^Avis d.impôt" --limit 1`
  then `impots download <id>`.

## Gotchas

- Plain HTTP clients get a Radware JS challenge on the login POST
  (`cfspart-idp.impots.gouv.fr/GetContexte`), so everything goes through Chrome.
- `documents.do?n=0` returns all years in one page (2017 onward); each year is
  rendered twice (tabs and accordions) and the parser deduplicates by token.
  Linked documents (accusés de réception, annexes 2042-C/3916, one avis de taxe
  foncière per property) are GET forms, not links; `linked: true` in `--json`.
  Measured: 67 documents on one account, 40 main ones downloaded in 12 s.
- File names are the readable label (accents kept, `/`→`-`, `13:27`→`13h27`, the
  property address appended), so an archive made with `download --all -o DIR`
  stays idempotent: reruns only fetch new documents.
- The site regenerates PDFs on every download (a "Date de téléchargement" line,
  internal timestamps): comparing an old copy with a fresh one needs the text,
  not a hash.
- Document ids are a hash of the site's `idEnsua` token. It was stable across
  requests within a session; whether it survives a new login has not been measured,
  so re-list before downloading by id in a new session.
- The messagerie is a JSF/PrimeFaces app (`/gaia2-zu-mapi/`). The CLI clicks its
  real DOM and waits for the PrimeFaces ajax queue. The site announced a
  "version modernisée" of the messagerie; if commands start failing on missing
  elements, the layout changed.
- Closed demandes ("Demande traitée") cannot be answered; a follow-up is a new
  demande (`send`). There is no reply command.
- `send` refuses topics whose form has extra fields beyond text + attachments, rather
  than guessing them. Topics 14, 43 and 4 were validated in dry run.
- Attachments: PDF, JPEG, TIFF, PNG, XLS, XLSX, ODS, ODT only, 20 MB in total; the
  site accepts alphanumeric file names only, so files are uploaded under a cleaned
  name (`Relevé d'impôt (v2).pdf` → `Relevedimpotv2.pdf`).
- `message` marks the opened demandes as read on the site.
