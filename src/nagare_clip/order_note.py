"""Say plainly when the finished video is not in shooting order.

Deterministic and free — no LLM call — in the same spirit as the cut report: the machine states what happened and the
human decides whether it was right.

A reorder changes the shape of the finished video more than any other single
decision, and the failure mode to avoid is a human noticing it only while
watching the result.  Relying on a model to mention it is not enough, so this
is computed from the resolved order itself.
"""

from __future__ import annotations

from collections.abc import Sequence

from nagare_clip.order import Segment, segment_label

HEADING = "## order of the finished video"


def format_order_note(
    order: Sequence[Segment],
    shooting: Sequence[Segment],
    problems: Sequence[str] = (),
) -> str:
    """The markdown block, or ``""`` when there is nothing to report.

    Nothing is reported when the resolved order IS shooting order — including a
    plan that states shooting order explicitly, which is not a reorder.  A
    *rejected* order is reported even though the resolved order is shooting
    order, because silence there would look like a plan that asked for nothing.
    """
    if problems:
        return (
            "\n".join(
                [
                    HEADING,
                    "",
                    "The order in force (`director/order.json`) does not "
                    "cover every line of every source exactly once, so it was **rejected "
                    "whole** and the finished video is in shooting order. Fix that `order` "
                    "and re-run from `intervals`, or re-run the director.",
                    "",
                    *(f"- {problem}" for problem in problems),
                ]
            )
            + "\n"
        )

    if list(order) == list(shooting):
        return ""

    was = {segment_label(s): i + 1 for i, s in enumerate(shooting)}
    lines = [
        HEADING,
        "",
        f"The finished video plays {len(order)} segment(s) in an order the director chose, "
        "not in shooting order. Nothing was added or dropped — the segments still "
        "cover every line of every source exactly once — but the sequence differs:",
        "",
    ]
    for position, segment in enumerate(order, start=1):
        label = segment_label(segment)
        before = was.get(label)
        if before == position:
            lines.append(f"{position}. {label}")
        elif before is None:
            lines.append(f"{position}. {label}  (a new split; not a whole source before)")
        else:
            lines.append(f"{position}. {label}  (was {before} in shooting order)")
    lines += ["", "Shooting order was:", ""]
    lines += [f"{i + 1}. {segment_label(s)}" for i, s in enumerate(shooting)]
    return "\n".join(lines) + "\n"
