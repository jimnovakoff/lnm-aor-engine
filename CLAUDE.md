# Working in this repo

A public repo. Keep it free of private details: no credentials, personal contact details or
references to private repos.

- **`data/*.json` is a published contract.** Briefing products fetch the raw files by URL, so
  file names and the output schema in README.md change only with every consumer in step. The
  weekly Action (`weekly.yml`) writes `data/`; don't hand-edit it.
- **No narrative text or positions in the output.** The flattened LNM text scrambles prose and
  coordinates; this is a safety posture, not a parsing gap (README.md, "Output schema").
- A new AOR is an entry in `aors.json`; test it against a local PDF
  (`python lnm_aor.py --aor <id> --pdf-file x.pdf`) before the weekly run picks it up.
- CI (`checks.yml`) compiles the parser and checks that `aors.json` and `data/*.json` parse.
