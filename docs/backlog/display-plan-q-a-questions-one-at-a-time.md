---
title: Display plan Q&A questions one at a time
state: available
category: polish
---

The plan-phase Q&A overlay currently shows all pending questions at once, which overwhelms the operator on plans with multiple clarifications. Rework the Q&A flow in the dashboard so questions render sequentially: show one question, accept the answer, advance to the next, repeat until the list is exhausted. Preserve the ability to skip or abort the overlay without losing already-entered answers. Keep the underlying plan-review state machine unchanged — this is a presentation-layer change in the curses overlay that hosts the prompt.
