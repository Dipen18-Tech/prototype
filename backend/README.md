# GRIT · Level Clearance Interview Scheduling

A working Django app for GRIT's interview scheduling. Students book their Level Clearance Interview, interviewers manage availability and record outcomes, and the interview team tracks who has booked and who hasn't.

**Live demo:** _[add your deployed link]_ · **Write-up (2 pages):** `GRIT_Interview_Scheduling_Writeup.pdf`

- **Django only.** Pages are server-rendered Django templates, every action is a plain HTML form, and validation uses Django forms. There is **no JavaScript** and no Node or React.
- **Real database.** SQLite locally; Postgres-ready through environment variables.
- **Real accounts and roles.** Students, interviewers and the ops team each sign in with their own account, and every page and action checks the user's role.
- **30 automated tests.** They cover the domain rules, permissions, CSRF, XSS escaping, double-booking and the full flows over HTTP.

---

## Run it locally (about 1 minute)

```bash
cd backend
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo          # demo students, interviewers, bookings
python manage.py runserver
```

Open <http://127.0.0.1:8000>. The login page has one-click demo accounts, and all of them use the password `grit-demo-2026`.

Run the tests:

```bash
python manage.py test interviews
```

## Deploy on Replit

1. Import the whole project folder into a Python Repl. The `.replit` file at the root already holds the run command: install, migrate, collect static files, seed if empty, then start gunicorn.
2. In **Secrets**, set the following:
   - `DJANGO_SECRET_KEY`: a long random string, which you can generate with `python -c "import secrets; print(secrets.token_urlsafe(50))"`
   - `DJANGO_DEBUG=0`
3. Click Run, then Deploy.

The same commands work on any host that runs Python (Render, Railway, an EC2 box).

| Env var | Default | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | dev key | **Required** when `DJANGO_DEBUG=0` |
| `DJANGO_DEBUG` | `1` | `0` in production (secure cookies, HTTPS redirect, HSTS) |
| `DJANGO_ALLOWED_HOSTS` | localhost, Replit domains | Comma-separated host names |
| `GRIT_DEMO_MODE` | `1` | Demo login, simulated clock and reset. **Set to `0` for real students.** |
| `POSTGRES_DB` / `_USER` / `_PASSWORD` / `_HOST` | not set | Use Postgres instead of SQLite (also install `psycopg`) |

## 3-minute tour

| Try this | Sign in as |
|---|---|
| Book an interview: pick a date, pick a slot, confirm, see the boarding pass | **Aarav** (eligible) |
| Join a live interview (join opens 10 min before) | **Diya** |
| Record an outcome. The student's exact message previews as you choose, using only CSS. | **Priya**, then *Record outcome* on Meera or Arjun |
| Open or close slots, or block a day that has bookings (they're reassigned automatically) | **Priya**, then *Availability* |
| Who has booked vs who hasn't, then nudge the stragglers | **Ops**, then *Booking tracker* |
| Nullify a badge for malpractice | **Ops**, then *Integrity* |
| Before vs after the result (pass / retake / fail / nullified) | Anyone, then the **Before / After** tab |
| Unlock an interview by clearing the last skill | **Rahul**, then use the demo button |
| Watch cooldowns and SLAs play out | Clock **+1d / +7d** in the top bar |

## The 3 most important student messages (exact copy)

**1. Unlock.** Sent when the last required skill is cleared. Its job is to turn "eligible" into "booked".
> **You've unlocked your Novice interview**
> You've cleared all 4 skills for AI Product Mastery · Novice. One step left: a 30-minute Level Clearance Interview with the GRIT team. Pick a slot that suits you — interviews must be done by 31 Dec to count.
> [Book my interview]

**2. Booking confirmed.** Its job is to make sure the student shows up, prepared.
> **Interview booked: Fri, 13 Nov at 4:00 PM**
> AI Product Mastery · Novice · 30 min with Priya Raman. Join from your GRIT dashboard — the Join button goes live 10 minutes before. Keep your college ID and a working camera ready. Need to change it? You can reschedule twice, up to 4 hours before.
> [View interview details]

**3. Result: retake later.** This is the hardest one to write. It leads with "not yet", quotes the interviewer, and gives a date and an honest attempt count.
> **Not yet — you can retake from Thu, 19 Nov**
> You were close on AI Product Mastery · Novice. Your interviewer's note: "Good UI fundamentals, but you struggled to explain how your prompt pipeline handles bad model output. Practise adding validation and fallbacks to your Gen AI project." Your next attempt unlocks on Thu, 19 Nov. Attempts used: 1 of 3.
> [See full feedback]

All student copy lives in `interviews/messages.py`.

## How it's built

```
grit/settings.py            env-driven settings (secure defaults when DEBUG=0)
interviews/
  machine.py                the state machine: states, allowed transitions, next_state()
  utils.py                  shared helpers: policy numbers + badge requirements, IST time helpers,
                            errors, UI labels/icons, jobs middleware, template context
  models.py                 data model (matches the write-up), DB constraints
  services.py               every write: guards, permission checks, transactions, audit log
  selectors.py              read-side queries: dashboards, leaderboard, tracker rows
  messages.py               all student-facing copy
  forms.py                  input parsing + field validation
  views.py / urls.py        thin views: parse → call service → redirect (Post/Redirect/Get)
  templates/interviews/     server-rendered pages + shared partials
  management/commands/      seed_demo, run_jobs (for cron)
  tests/test_domain.py      30 tests
```

**Design decisions**

- **There's one state machine.** Each student × badge has one `Journey` with one `state`. `services._transition()` goes through `machine.next_state()`, so an illegal move raises an error instead of corrupting data. The *How it works* page renders its tables from that same file.
- **The database blocks double-booking.** A row lock on the slot, plus partial unique constraints, means a second live booking for a slot (or for a badge) is rejected even under a race. The losing student gets "That time was just taken".
- **Every write is one transaction and leaves a row in `JourneyEvent`.** The row records actor, from → to, and reason, so "why is my badge gone?" always has an answer.
- **Permissions live in the service layer.** A student can't touch another student's booking, and only the assigned interviewer can record an outcome, even if someone crafts the request by hand. Views add role checks on top.
- **Security.**
  - Django escapes all output. Interviewer feedback is shown to students, and there's a test for that.
  - CSRF protection is on for every form.
  - Internal notes are never rendered for students (also tested).
  - CSV export guards against formula injection.
  - Secure cookies, HSTS and an HTTPS redirect switch on in production.
- **No JavaScript.** Things that usually need JS use CSS `:has()` instead: hiding the rubric unless "Attended" is chosen, and showing the matching student-message preview. The server validates everything regardless.
- **Time-based jobs.** "Slot ended → awaiting outcome", "cooldown ended → eligible" and rolling interviewer availability forward run in `run_system()`. It's idempotent, is called lazily by middleware, and can also run from cron with `python manage.py run_jobs`.

## Production notes

- **Turn off demo mode** (`GRIT_DEMO_MODE=0`). This removes one-click login, the simulated clock and reset.
- **Use Postgres for real concurrency.** `select_for_update` takes real row locks there; SQLite serialises writes instead. Schedule `run_jobs` every minute and back the database up daily.
- **Send notifications externally too.** Messages are stored as *template key + data*, so the same copy can go out by email or WhatsApp without being duplicated.
- **Use the Django admin** at `/admin/`, signed in as `ops`, to correct data by hand. Every state change still appears in the audit log.
