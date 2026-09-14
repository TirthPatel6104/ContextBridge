# Privacy model

ContextBridge exists because people carry sensitive working context between AI tools. The design goal is simple: **you should always know where your data is and what left your machine.**

## Where data lives

| Data | Location | Leaves the machine? |
|---|---|---|
| Original transcripts, exports, PDFs | Wherever you keep them. ContextBridge reads them and never modifies them. | No |
| Extracted memory packages (all versions) | `~/.contextbridge/contextbridge.db` (SQLite) or `~/.contextbridge/<package>/` (legacy JSON). Override with `CB_STORAGE_DIR`. | No |
| Attached files (text extracted from files you attach to a package) | `~/.contextbridge/attached_files/<package>/` | Only inside a prompt you explicitly generate with "include files" |
| Watch folder | `~/.contextbridge/watch/` | Only inside a prompt you explicitly generate with "include watch folder" |
| API keys | Environment variables / `.env` | Sent only to the vendor they belong to, by that vendor's SDK |

The dashboard binds to `127.0.0.1` and loads no fonts, scripts, or images from the internet.

## What can leave the machine, and when

1. **Extraction with a vendor engine.** If you choose `openai` or `claude` as the extraction engine, the transcript is sent to that vendor to be turned into structured memory. The default engine (`local`) is a rule-based extractor that runs entirely offline; Ollama models also run locally.
2. **Querying with `cb query`.** The question and the selected memory are sent to the model you name.
3. **Pasting a prompt.** That is the whole point of the tool, and it is always a manual step you perform.

Nothing is sent anywhere by the dashboard, the extension, or the CLI without one of those explicit choices. There is no telemetry.

## Redaction

Before memory is stored or exported, a detection pass looks for:

| Kind | Examples |
|---|---|
| `private_key` | `-----BEGIN ... PRIVATE KEY-----` blocks |
| `api_key` | OpenAI, Anthropic, GitHub, AWS, Slack, Google, Stripe, Hugging Face key formats |
| `jwt` | `eyJ...` tokens |
| `credential_assignment` | `password = ...`, `api_key: ...`, `token is ...` |
| `credential_url` | `scheme://user:password@host` |
| `credit_card` | 13–19 digit numbers passing the Luhn check |
| `ssn` | US Social Security numbers |
| `email`, `phone`, `ip_address` | Contact details and addresses |

Redaction is **on by default** and is transparent:

* Matched spans are replaced with `[REDACTED:KIND]`; the item records only `{kind, count}`, never the value.
* The import result and the review step show what was masked and in which items.
* Your original files are untouched. If a detector produced a false positive, re-import with redaction off (`--no-redact`, or the checkbox in the dashboard) — that is a deliberate, per-import choice.
* You can restrict which detectors run (`RedactionPolicy(kinds=[...])`) or turn the default off with `CB_REDACT=false`.

Detection is pattern-based. It catches common formats and is measured in the evaluation suite (see [EVALUATION.md](EVALUATION.md)), but it will miss unusual secrets and can occasionally flag harmless strings. Review the extracted items before you paste them somewhere.

## Logging

Log lines record counts, package names, and exception class names. They never include transcript text, memory content, prompts, or API keys. Unexpected errors in the web app return a generic message to the browser.

## Local API surface

The Flask server is meant to be reached from the dashboard page and from the Chrome extension running on `chatgpt.com` / `claude.ai`. Cross-origin requests from any other website are rejected by CORS, so a malicious page cannot read your packages through your browser. If you change `CB_HOST` to expose the server on a network, put it behind authentication — it has none of its own.

## Deleting data

* `cb delete <name>` or the dashboard's delete button removes every version of a package.
* Remove `~/.contextbridge/` to delete everything, including attached files.
* Rolling back a version does not delete the later versions from SQLite (they remain retrievable by version number). Delete the package if you need them gone.
