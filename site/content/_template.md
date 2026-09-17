---
title: The claim in one line, naming the company and the metric
date: 2026-09-21
company: CRWV
summary: One sentence a reader can act on without opening the page.
status: draft
tags: first-topic, second-topic
---

<!--
How to use this file
- Copy it to site/content/YYYY-MM-DD-short-slug.md. Files starting with "_" are never built.
- `company` is a ticker (CRWV, NBIS, NVDA, MSFT, GOOGL, AMZN, META) or `industry`.
- Keep `status: draft` until the numbers are final; the builder skips drafts. Set
  `status: published` to ship. Delete these guidance comments before publishing.
- Every number should trace to a filing (form, date, page) or to a named cell in the workbook.
- The last two sections are required. Keep their headings exactly as written.
-->

## The question

<!-- One paragraph: what is being asked, why it matters now, and the single number that answers it. -->

## What the model says

<!-- The headline result with its period and unit, then the one chart or table that carries it.
     Link the workbook: https://github.com/<owner>/ai-economics/blob/main/models/<TICKER>.xlsx -->

## Key drivers

<!-- The three or four inputs the answer is most sensitive to, each with its source
     (filing and page, transcript, or public price quote). -->

## Sensitivities

<!-- A small table: driver | low | base | high | effect on the headline number. -->

## Position

<!-- One paragraph stating the call plainly: what you believe, by when, and how confident you are.
     A position that cannot be wrong is not a position. -->

## What would prove this wrong

<!-- One row per claim, added under the separator line. The shape of a row (an example, not a call):
     | <the position in one sentence> | <the reported figure that would make it wrong> | YYYY-MM-DD |
-->

| claim | falsifying number | deadline |
|---|---|---|

Copy each row above into `calls.md` at the repo root with today's date and outcome `open`; update the outcome (right / wrong / partial) when the deadline passes.
