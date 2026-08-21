# Snapshot Skill candidates for stoppable Runs

Before a Skill Builder Run may mutate candidate files, its Skill Design Operation receives a durable, bounded baseline copy that is removed after terminal settlement. A stopped, failed, timed-out, or interrupted Run restores that baseline, while only a successful candidate or clarification keeps the Run's mutations; this avoids exposing half-authored packages without turning candidate files into a general version-history system or storing large file bodies in operation JSON.
