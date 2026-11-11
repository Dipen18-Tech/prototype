# Loom talk track (~6 minutes)

Talk through this in your own words; don't read it out. Before you record, open the app, click **Reset** in the top bar, and keep the write-up PDF open in another tab. Use the "Signed in as" switcher in the top bar to change personas.

---

### 0:00 – 0:40 · Framing the problem
- "GRIT students clear skill contests, then a live Level Clearance Interview earns them the badge. A small interview team, thousands of students, and a hard **December badge deadline**."
- "Before designing screens, I asked one question: *what state is this student in, and what moves them forward?* Everything else follows from that."

### 0:40 – 1:40 · The state machine (write-up page 1)
- Walk the diagram: Skills in progress → Eligible → Booked → Awaiting outcome → Passed. Then the three non-pass exits (Retake, Fail, No-show) loop back through a cooldown. Integrity sits on top: On hold, then Nullified.
- "Pass, Retake later and Fail differ in **consequence**, not just in label: 7 days versus 21 days of cooldown, a different tone, and feedback that's mandatory."
- "In the code this is one file, `machine.py`. The service layer refuses any transition that isn't listed there, and the How it works page renders from it, so the doc can't drift from the product."

### 1:40 – 3:00 · Student flow
- **Aarav** (eligible): show the hero card answering *what's my status, what do I do next*, including the deadline countdown and slots this week.
- Book: date strip, pick a slot, confirm. The checkbox is there to reduce no-shows. You land on the **boarding pass**, which plays on GRIT's "quick layover" travel theme.
- **Inbox:** show the confirmation message. "I wrote the copy for the three moments that matter most: unlock, confirmed, and *not yet*."
- **Diya**: the interview is live and the Join button is active.

### 3:00 – 4:00 · Interviewer
- Priya's agenda shows past interviews needing an outcome, a live one, upcoming ones, and open slots.
- Record an outcome for Meera: rubric, then Retake later, then type feedback. **Point at the live preview**: "the interviewer sees exactly what the student will read. That's how you stop feedback like 'needs improvement'."
- Availability: toggle a slot. Block a day that has bookings. "It tries to move each booking to a free colleague at the same time first, and only cancels with priority rebooking if nobody is free."

### 4:00 – 5:00 · Interview Ops
- Overview: eligible but not booked, awaiting outcome, capacity versus demand (it respects each interviewer's daily cap), and the funnel.
- Booking tracker: filter to "Eligible · not booked" and **Nudge**. "This is the brief's ask: who has booked and who hasn't, without a spreadsheet."
- Integrity: confirm a flag on a passed student. Their badge is nullified, Miles are reversed and the leaderboard is updated. Show the **audit log**.

### 5:00 – 5:40 · Before / After
- Switch between Pass, Retake and Nullified. "Same components, before and after the result, across the home card, badge shelf, leaderboard, inbox and the ops row."

### 5:40 – 6:15 · Edge cases & how I worked
- Name 3 or 4 edge cases quickly: two students racing for one slot (demo it with two browsers if time allows), call drops, a cooldown that crosses the deadline, a retake going to a different interviewer.
- "It's a real Django app: server-rendered templates, real logins per role, and the database itself refuses double bookings. I used AI tools to move fast, but I owned the logic. 30 automated tests cover races, permissions, cooldowns, attempts, flags and XSS, and testing caught real gaps: availability ran out after long cooldowns, so interviewer availability became a rolling weekly pattern."
- Close with the **open questions** I'd take to the GRIT team: Fail vs Retake consequences, the attempt limit, the deadline policy, and re-attempts after nullification.
