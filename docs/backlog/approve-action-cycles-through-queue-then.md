---
title: Approve action cycles through queue then returns to list
state: available
category: feature
---

In review mode, pressing approve on an item should advance to the next awaiting_review item rather than dropping back to the main list immediately. Once the approve queue is empty, the dashboard returns to the main list view. Current behavior (based on `agentor/dashboard/modes.py` review handling) appears to exit review mode after a single approval. Verify actual behavior before implementing and ensure the cycle respects feedback/reject actions consistently.
