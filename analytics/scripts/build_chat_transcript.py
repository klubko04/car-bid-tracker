"""Rebuild the .cc-discussion log as a VERBATIM copy of the chat.

No summarising. User prompts, assistant output, tool calls and tool results are
copied exactly as they appear in the session JSONL.

Thinking blocks are present in the log (357 of them) but their text is EMPTY —
only a cryptographic signature is persisted. They are emitted as explicit
placeholders rather than reconstructed, because reconstructing them would mean
inventing a record of reasoning that was never saved.
"""
import json
import pathlib
import re

T = ("/home/striker/.claude/projects/-home-striker-projects-car-bid-tracker/"
     "1de5f512-27cc-4a6f-a0ee-152360548399.jsonl")
OUT = pathlib.Path("/home/striker/projects/car-bid-tracker/.cc-discussion/"
                   "Build analytics pipeline script from test files.md")

SKIP_TYPES = {"queue-operation", "ai-title", "last-prompt", "mode",
              "file-history-snapshot", "file-history-delta", "system"}

# `attachment` records are a mix of real chat content and harness plumbing.
# These four ARE content — they appeared in the conversation and change what was
# being discussed — so they are rendered in position:
#   edited_text_file       the user edited a file outside the chat, mid-session
#   file                   a file was attached into context
#   compact_file_reference a file carried across a context compaction
#   date_change            the session crossed midnight
# Everything else (todo_reminder, deferred_tools_delta, agent_listing_delta,
# skill_listing) is tooling bookkeeping, not conversation, and is skipped.
ATTACH_KEEP = {"edited_text_file", "file", "compact_file_reference", "date_change"}

NOISE = re.compile(
    r"<(system-reminder|ide_selection|ide_opened_file|local-command-stdout|"
    r"local-command-caveat|command-name|command-message|command-args)>.*?</\1>",
    flags=re.S)


def fence(text, lang=""):
    """Fence that cannot be broken by backticks inside the payload."""
    text = "" if text is None else str(text)
    bt = "```"
    while bt in text:
        bt += "`"
    return f"{bt}{lang}\n{text}\n{bt}"


def clean_user(t):
    t = NOISE.sub("", t)
    return t.strip()


def blocks_of(rec):
    c = rec.get("message", {}).get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return c if isinstance(c, list) else []


# ---------------------------------------------------------------- collect
records = []
for line in open(T, encoding="utf-8"):
    try:
        d = json.loads(line)
    except Exception:
        continue
    if d.get("type") in SKIP_TYPES:
        continue
    if d.get("type") == "attachment":
        a = d.get("attachment") or {}
        if a.get("type") not in ATTACH_KEEP:
            # keep a todo list only when it actually has items
            if not (a.get("type") == "todo_reminder" and a.get("itemCount")):
                continue
    records.append(d)

# tool_use id -> result payload, so a call and its output sit together
results = {}
for d in records:
    for b in blocks_of(d):
        if isinstance(b, dict) and b.get("type") == "tool_result":
            cc = b.get("content")
            if isinstance(cc, list):
                cc = "\n".join(x.get("text", "") for x in cc
                               if isinstance(x, dict) and x.get("type") == "text")
            results[b.get("tool_use_id")] = cc if isinstance(cc, str) else json.dumps(cc, indent=2)

HEAD = """---
title: Build analytics pipeline script from test files
project: car-bid-tracker
tool: Claude Code (Opus 5)
started: 2026-08-12
updated: 2026-08-16
status: ongoing
type: chat-transcript
tags: [car-bid-tracker, apibara, iaai, salvage-auction, analytics-pipeline, web-scraping, csv-schema, images]
---

# Build analytics pipeline script from test files

**Verbatim copy of the chat.** User prompts, assistant output, tool calls and tool
results are reproduced exactly as recorded in the session log — nothing summarised,
nothing paraphrased, nothing reordered.

> [!warning] Thinking blocks are not recoverable
> The session log contains 357+ thinking blocks, but each stores an **empty**
> `thinking` string plus a cryptographic `signature` and nothing else. Verified
> twice: by measuring every block (0 characters of thinking text across all of
> them), and by walking every field of a full assistant record — the only long
> string anywhere in it is the signature. The reasoning text is never written to
> disk.
>
> They appear below as `*[thinking block — content not retained in the session
> log]*` placeholders, positioned where the thinking happened. They are **not**
> reconstructed: writing them from memory would fabricate a record of reasoning
> that was never saved, which in a reference document is worse than a gap.

> [!info] One-message lag, which self-heals
> This file is regenerated from the session log after every run. A turn's closing
> response is only written to that log once the turn ENDS, so each rebuild
> contains everything up to and including the **previous** response, and the
> current one arrives with the next rebuild. Nothing is lost — it just trails by
> one message.

Source: `~/.claude/projects/-home-striker-projects-car-bid-tracker/1de5f512-27cc-4a6f-a0ee-152360548399.jsonl`

---

"""

body = []
prompt_n = 0
pending_header = False

for d in records:
    kind = d.get("type")
    bs = blocks_of(d)

    if kind == "attachment":
        a = d.get("attachment") or {}
        at = a.get("type")
        if at == "date_change":
            body.append(f"\n*[date changed to {a.get('newDate')}]*\n")
        elif at == "edited_text_file":
            body.append(f"\n<details>\n<summary>*[user edited "
                        f"{a.get('filename')} outside the chat]*</summary>\n\n"
                        f"{fence(a.get('snippet') or '')}\n</details>\n")
        elif at == "compact_file_reference":
            body.append(f"\n*[file carried across compaction: "
                        f"{a.get('displayPath') or a.get('filename')}]*\n")
        elif at == "file":
            c = a.get("content") or {}
            inner = (c.get("file") or {}) if isinstance(c, dict) else {}
            body.append(f"\n<details>\n<summary>*[file attached: "
                        f"{a.get('displayPath') or a.get('filename')}]*</summary>\n\n"
                        f"{fence(inner.get('content') or '')}\n</details>\n")
        elif at == "todo_reminder":
            body.append(f"\n<details>\n<summary>*[todo list — "
                        f"{a.get('itemCount')} items]*</summary>\n\n"
                        f"{fence(json.dumps(a.get('content'), indent=2), 'json')}\n</details>\n")
        continue

    if kind == "user":
        # a user record is either a real prompt or a tool_result carrier
        texts = [b.get("text", "") for b in bs
                 if isinstance(b, dict) and b.get("type") == "text"]
        joined = clean_user("\n".join(texts))
        if not joined:
            continue
        prompt_n += 1
        body.append(f"\n## Prompt {prompt_n}\n\n{fence(joined)}\n\n### Response\n")
        continue

    if kind != "assistant":
        continue

    for b in bs:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "thinking":
            body.append("\n*[thinking block — content not retained in the session log]*\n")
        elif bt == "text":
            txt = (b.get("text") or "").strip()
            if txt:
                body.append("\n" + txt + "\n")
        elif bt == "tool_use":
            name = b.get("name", "?")
            inp = b.get("input") or {}
            label = (inp.get("description") or inp.get("file_path")
                     or inp.get("prompt") or inp.get("query") or inp.get("skill") or "")
            label = str(label).splitlines()[0][:90] if label else ""
            head = f"**Tool — {name}**" + (f": {label}" if label else "")
            # command / body first, as the chat shows it
            if name == "Bash":
                payload = fence(inp.get("command", ""), "bash")
            elif name in ("Write",):
                payload = fence(inp.get("content", ""))
            elif name in ("Edit",):
                payload = ("*old_string*\n" + fence(inp.get("old_string", "")) +
                           "\n*new_string*\n" + fence(inp.get("new_string", "")))
            else:
                payload = fence(json.dumps(inp, indent=2), "json")
            res = results.get(b.get("id"))
            out = f"\n*Result*\n{fence(res)}\n" if res else ""
            body.append(f"\n<details>\n<summary>{head}</summary>\n\n{payload}\n{out}\n</details>\n")

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(HEAD + "".join(body), encoding="utf-8")
txt = OUT.read_text(encoding="utf-8")
print(f"wrote {OUT}")
print(f"  {prompt_n} prompts, {len(txt):,} chars, {len(txt.splitlines()):,} lines, "
      f"{OUT.stat().st_size/1e6:.2f} MB")
