---
name: web-brief
description: Turn a URL or a topic into a tight, skimmable brief — one headline, three key points, one "so what" line. Use when a user pastes a link, or asks for the gist / tl;dr / a quick summary of something. Produces chat-friendly output in a fixed 1-3-1 shape.
version: 1.0.0
license: MIT
metadata:
  hermes:
    tags: [research, summarization, communication]
---

# Web Brief

Produce a short, structured brief from a URL or a topic. Output is delivered into a
chat channel (Telegram / Slack / Discord), so it must be tight and skimmable.

## When to use
- The user pastes a link and asks what it says.
- The user asks for "the gist", "tl;dr", or a quick summary of a topic.

## Steps
1. **Get the content.**
   - If a URL was given: fetch it. Prefer a scraping/search tool if one is enabled
     (Firecrawl, Tavily, or Parallel); otherwise use the built-in fetch.
   - If only a topic was given: search first, then read the top result.
2. **Find the spine** — the single claim the source is actually making. Everything
   else is support or qualification.
3. **Write the brief** using the exact template in
   [`references/format.md`](references/format.md). Do not add anything outside it.

## Rules
- Exactly: one headline line, three bullets, one "So what" line. Nothing more.
- No preamble. Do **not** start with "Here is a summary" — start with the headline.
- The headline is a *claim* (≤12 words), not a topic label.
- If you could not fetch the source, say so in one line instead of guessing.
