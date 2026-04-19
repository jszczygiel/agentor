---
title: Rework conflict summary to lead with feature context
state: available
category: polish
---

When a merge fails, `committer.py` transitions the item to CONFLICTED and stores a summary in `last_error`. The current summary emphasizes merge mechanics; operators want it reframed so the bulk describes the original feature/item being integrated, with only a short tail covering the merge conflict itself. Update the summary builder so feature context (title, intent, branch) dominates and the merge-failure detail is a brief trailing note. Exact wording and length split unclear from the note — confirm with operator before finalizing format.
