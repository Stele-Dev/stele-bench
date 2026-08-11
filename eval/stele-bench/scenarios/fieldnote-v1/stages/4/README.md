# Fieldnote

Fieldnote 1.0 is a single-file Python 3.11 command-line diagnostic summarizer.
It uses only the standard library, performs no network access, and has no
resident or watch mode.

```sh
python3 fieldnote.py events.jsonl --format json
python3 fieldnote.py events.jsonl --format html -o report.html
```

Reports normally stay below 50 KiB. `--include-events` is the explicit
exception: it embeds every sanitized event and may exceed that limit. Keys
named `token`, `password`, or `secret` are always replaced with `[REDACTED]`.
