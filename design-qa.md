# Host execution approval card design QA

Date: 2026-08-15

Reference: user-provided `1790 × 430` approval-card screenshot. The reference and
the live ActWeave screenshot were reviewed in one vertically stacked comparison
input at the same 1790 px viewport width.

## Live states and viewports

- Desktop `1790 × 900`: pending card measured `784 × 406` inside the product's
  bounded chat column. The amber border, tinted status header, circular status
  mark, large radius, plain full-command preview, and deny/allow hierarchy match
  the reference while retaining ActWeave's required host-risk and execution-domain
  details.
- Mobile `390 × 844`: card measured `328 px` wide with `scrollWidth ==
  clientWidth`; warning text, metadata, command, countdown, and actions had no
  horizontal overflow. The card can be scrolled to expose its status header and
  both actions without clipping.
- Pending, denied, expired, and finished states were exercised in the live
  browser. Pending actions became unavailable immediately after a decision, and
  terminal cards remained read-only.

## Findings and fixes

| Severity | Finding | Resolution |
| --- | --- | --- |
| P2 | Mobile decision buttons rendered at 36 px high, below a reliable touch target. | Both buttons now use a 44 px mobile minimum height and retain the compact desktop height. Live remeasurement: `286 × 44` for each button. |

No open P0, P1, or P2 visual findings remain.

final result: passed
