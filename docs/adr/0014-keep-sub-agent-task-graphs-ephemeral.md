---
status: accepted
---

# Keep Sub-Agent Task graphs ephemeral while persisting context evidence

Each Sub-Agent Task execution receives its own Context Subject, Context Evidence, Projection Head, final Provider-request guard, and automatic compaction, keyed by its internal `execution_id`. Its Agent Graph state remains in memory for that Task lifetime and is not given an independent Checkpoint or resume contract. Terminal Context Evidence and the settled Context Projection remain durable; a Worker or Task retry creates a new `execution_id` rather than reopening the earlier Task graph.

This preserves the domain rule that a Sub-Agent Task is delegated work inside its parent Run rather than a child Run. It accepts that an interrupted Task cannot resume its private message history in exchange for avoiding a second durable graph lifecycle, checkpoint retention model, and settlement authority inside every delegated execution; sibling and retry executions remain independently measurable and auditable.

The Lead Context Usage surface reports only the Lead Thread. Each Sub-Agent Task surface may expose its own live or settled Context Projection, and no aggregate adds sibling Task Context Windows together. Only a Task result that actually enters the Lead Agent's messages contributes to the Lead Context Window; the Task's private history and Provider usage never do.

Each Task inherits the parent Run Snapshot's frozen summarization enablement, summary model, Prompt, keep policy, and hierarchical-call limits without consulting current platform configuration. Trigger fractions and Token limits are evaluated against that Task's own context model and capacity, and every `execution_id` compacts independently. Summary-model calls are not tool calls and do not consume the Task's ToolCallControl limit; disabled summarization remains disabled, leaving the final Provider-request guard to reject an over-capacity request.

Sub-Agent Scheduler loops do not receive or create an application database Session. They submit typed Evidence commands through a thread-safe Context Evidence Observer owned by the parent Run loop and wait for durable acknowledgement. The owner batches preparation and dispatch before the external call, commits Provider observations and Head changes after return, and enforces an Evidence barrier before Task settlement; loss of the parent Execution Lease rejects later writes rather than letting the Task bypass authority.
