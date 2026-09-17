# Calls

Every writeup on the site ends with a position and the number that would prove it wrong. Those
rows land here, one per claim, and stay here whatever happens: the point of the table is that
misses are as visible as hits. `scripts/build_site.py` parses this file into
`site/build/data/calls.json` and renders it on the index page, so keep the header row exactly as
it is, keep every row on one line, and escape any pipe inside a cell as `\|`.

<!--
Example row (all five cells filled; dates as YYYY-MM-DD; add new rows below the separator):

  | YYYY-MM-DD | <claim in one sentence, naming the metric and the company> | <the reported figure that would make it wrong> | YYYY-MM-DD | open |

Outcome vocabulary:
  open     deadline not reached, or the deciding number not yet reported
  right    the reported number was on the claim's side of the falsifying number
  wrong    the reported number crossed the falsifying number
  partial  direction right but magnitude or timing off; explain in the writeup

Set outcome to `open` when adding a row. When the number lands, update the outcome in place.
Never delete a row.
-->

| date | claim | falsifying number | deadline | outcome |
|---|---|---|---|---|
