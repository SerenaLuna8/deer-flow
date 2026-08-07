Produce exactly two segments from this conversation, in this order: a task-continuity summary, then tagged memory facts.

Segment 1 — continuity block. Output exactly one block wrapped in <continuity> and </continuity> containing free prose written for an agent that resumes this thread after the summarized messages are gone. Cover: the current goal, what is already done, what is in progress, key decisions with their reasons, and the immediate next steps. Prefer concrete names — files, commands, identifiers, exact values — over vague references. If an existing summary is provided, return one complete replacement that covers both it and the new conversation segment. Keep the block content within 2000 characters. The continuity block must never be empty.

Segment 2 — tagged facts. Immediately after the closing </continuity> tag, extract key facts worth remembering beyond this thread. For each fact, annotate its memory attributes.

Only SNIP facts deserve a non-[skip] mark:
- Signal: would the user need to repeat this if forgotten?
- Novel: not just a restatement of another fact in this same conversation chunk
- Important: prevents rework or captures preferences / rules
- Persistent: still relevant after 2 weeks

Output one fact per line in this format:
- [mark] fact content

Marks (choose the best match):
- [permanent] Core preferences, personal traits, habits — never becomes stale
- [durable] Technical discoveries, project knowledge, config details — valid for months
- [ephemeral] Active task state, temporary decisions — may change in weeks
- [correction] Correction to a previous memory — state what changed
- [skip] Does not meet SNIP criteria, is conversational filler, is code/source facts derivable from the repo, or is only useful as an audit breadcrumb

Priority: user corrections and preferences > solutions > decisions > events > environment facts. The most valuable memory prevents the user from having to repeat themselves.

Do not mark something [skip] merely because it might already exist in long-term memory; Dream handles long-term-memory deduplication later.

Keep the tagged segment to concise bullet lines only — no preamble, no commentary, no prose outside the continuity block.
If nothing in the new conversation segment is worth remembering, the tagged segment must be the single line: (nothing)
Keep the tagged segment within 1000 characters. When space is limited, retain corrections and preferences first, then permanent facts, durable decisions/solutions, and only the newest active ephemeral state. Drop stale events, environment details, and skip items first.

The input contains a Previous Summary and a New Conversation Segment.

Input:
{messages}
