---
title: Split approve and feedback actions in review
category: polish
state: available
---

In the dashboard review mode, approve currently overloads "accept as-is" and "accept with feedback" into one flow. Separate these into two distinct actions: a pure approve that proceeds straight to commit/merge with no prompt, and a separate action for attaching feedback before approving. Feedback capture should not be in the approve path. Exact key bindings TBD — likely keep `a` for approve and add a dedicated key (e.g. `f`) for feedback. Touches `agentor/dashboard.py` review mode handlers and any committer wiring that assumes feedback is always solicited.
