# Privacy model

ContextBridge exists because people carry sensitive working context between AI tools. The design goal is simple: **you should always know where your data is and what left your machine.**

## Where data lives

| Data | Location | Leaves the machine? |
|---|---|---|
| Original transcripts, exports, PDFs | Wherever you keep them. ContextBridge reads them and never modifies them. | No |
| Extracted memory packages (all versions) | `~/.contextbridge/contextbridge.db` (SQLite) or `~/.contextbridge/<package>/` (legacy JSON). Override with `CB_STORAGE_DIR`. | No |
| Egress ledger (which item ids were rendered for which model, when) | Same database (`egress_log` table) or `~/.contextbridge/<package>/egress.jsonl`. Holds ids and counts, never memory text. | No |
| Attached files (text extracted from files you attach to a package) | `~/.contextbridge/attached_files/<package>/` | Only inside a prompt you explicitly generate with "include files" |
| Watch folder | `~/.contextbridge/watch/` | Only inside a prompt you explicitly generate with "include watch folder" |
| API keys | Environment variables / `.env` | Sent only to the vendor they belong to, by that vendor's SDK |

The dashboard binds to `127.0.0.1` and loads no fonts, scripts, or images from the internet.

## What can leave the machine, and when

1. **Extraction with a vendor engine.** If you choose `openai` or `claude` as the extraction engine, the transcript is sent to that vendor to be turned into structured memory. The default engine (`local`) is a rule-based extractor that runs entirely offline; Ollama models also run locally.
2. **Querying with `cb query`.** The question and the selected memory are sent to the model you name.
3. **Pasting a prompt.** That is the whole point of the tool, and it is always a manual step you perform.

Nothing is sent anywhere by the dashboard, the extension, or the CLI without one of those explicit choices. There is no telemetry.

## Sharing boundaries

Redaction protects against *patterns* (keys, emails). Sharing boundaries protect *facts you know are sensitive* but that no pattern would catch: a salary, an unreleased product name, a colleague's health situation, a client's name.

Every memory item carries a sharing policy:

| Policy | Rendered for a local model (Ollama) | Rendered for ChatGPT / Claude | Kept in your memory |
|---|---|---|---|
| `any` (default) | Yes | Yes | Yes |
| `local_only` | Yes | **No** | Yes |
| `never` | **No** | **No** | Yes |

The policy is enforced at the only place memory turns into a prompt: `build_prompt` (CLI, dashboard, extension) and the context for `cb query`. Before retrieval runs, the memory is partitioned by the target's kind; withheld items cannot be selected by a query, cannot appear in the "full package" scope, and are not counted in the footer's item total. What *was* withheld is always shown to you (CLI output, dashboard panel, extension status line), so a silent omission is impossible.

Target kind is decided by name. Ollama-style names (`local`, `ollama`, `llama3`, `mistral`, …) are local; everything else, including a name ContextBridge has never seen, is treated as **cloud**. That default errs on the side of withholding.

Setting a policy is a versioned package change, so it shows in `cb history` and can be rolled back. Policies travel with portable exports and survive merges and re-imports (a re-stated `never` item stays `never`).

## Egress ledger

Every time memory is rendered for a model, ContextBridge appends one record to a per-package ledger:

* when, which package and version, which target, whether that target is local or cloud
* which surface asked: `cli`, `dashboard`, `extension`, or `query`
* the retrieval query, if one was used
* the **ids** of the items rendered, how many were withheld, and the estimated token count

The ledger never stores the rendered text or the memory content, so it is not a second copy of your memory. `cb audit <name>` (or the dashboard's *Ledger & health* section) shows the events and, per item, which targets have received it and whether any of them was a cloud model. `cb health` uses the same data to report how many items have ever reached a cloud model and how many have never left the machine.

Previews are not recorded: `cb prompt --dry-run` and the dashboard's *Record this in the egress ledger* checkbox (off) build the prompt without a ledger entry. `cb audit <name> --clear` or `DELETE /api/packages/<name>/audit` deletes the ledger for a package; deleting the package deletes its ledger too.

The ledger records what ContextBridge *rendered*. Whether you then pasted it, and what the vendor did with it afterwards, is outside its knowledge.

## Hand-offs

A prompt you generate carries a footer naming the package and version. When you later import the conversation that began with that prompt, the pasted block is recognised and skipped, so the extractor sees only what you and the model said afterwards. This avoids re-ingesting your own context as if it were new evidence (which would inflate "seen" counts and could re-introduce items you had removed) and records the hand-off in package metadata. Nothing about this leaves the machine.

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

* `cb delete <name>` or the dashboard's delete button removes every version of a package and its egress ledger.
* `cb audit <name> --clear` removes only the ledger.
* Superseded and done items are *not* deleted by their status change; use `cb remove` if the content itself must go.
* Remove `~/.contextbridge/` to delete everything, including attached files.
* Rolling back a version does not delete the later versions from SQLite (they remain retrievable by version number). Delete the package if you need them gone.
