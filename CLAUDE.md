# CLAUDE.md

## What Is This Project

Building a personal training coach that:
1. Reads training data from a Google Sheet (synced with user's Drive)
2. Analyzes progress intelligently
3. Sends daily coaching emails via Gmail
4. Adapts to the user's needs over time

## First Steps

**Before writing ANY code, read `INITIAL_CONTEXT.md`.**

This file contains the history of how this project started - a long conversation about training, coaching, and what the user actually wants. It's context, not a specification.

## Your Role

You are planning and building this WITH the user, not FOR them.

- Ask questions when unclear
- Propose ideas, don't assume
- Start simple, add complexity as needed
- The user knows their training; you know how to build systems

## Key Requirements (from the conversation)

1. **Minimal daily input** - User marks exercises as done, optionally adds notes
2. **Flexible input** - Handle both "✓" and long paragraphs with questions
3. **Intelligent analysis** - Detect trends, stalls, project outcomes
4. **Honest coaching** - No pandering, direct feedback
5. **Scalable** - Should work for years of data
6. **Google ecosystem** - Sheet in Drive, emails via Gmail

## What's NOT Decided

- Exact architecture (multi-agent was an idea, not a decision)
- Sheet structure (depends on user's actual program)
- Email format and frequency
- How to handle different program lengths (not always 30 weeks)
- Compression and archiving strategy

## User's Current Situation

- Has a strength training program (Excel file)
- Currently Week 7 of current program
- Based in Spain, speaks Spanish and English
- Works long hours, travels frequently
- Wants direct, honest coaching

**But this changes.** Goals change. Programs change. Life changes. The system must adapt.

## Tech Stack (Proposed)

- Python
- Google Sheets API (gspread)
- Gmail API (for sending emails)
- Claude API (for intelligence)
- Everything synced with user's Google account

## How To Start

1. Understand what the user actually has (their Excel/Sheet)
2. Understand what they want the daily email to look like
3. Design the simplest version that works
4. Build it
5. Iterate based on feedback

## Files In This Project

```
strength-coach-agent/
├── CLAUDE.md              # This file
├── INITIAL_CONTEXT.md     # History of the conversation (read first)
├── requirements.txt       # Python dependencies
├── .gitignore
├── src/                   # Code goes here (empty, build together)
└── config/                # Credentials go here (not in git)
```

## Communication

- Code and docs: English
- User-facing output (emails): Spanish (unless user prefers otherwise)
- When in doubt: Ask the user

## How I Work (Standing Rules)

These apply every session without being asked:

### Git
- After any meaningful change, commit and push to `main` automatically
- Write clear commit messages focused on why, not just what
- Never push without confirming if the change is destructive or large in scope

### Proactive behavior
- If I notice a bug, missing env var, broken workflow, or obvious improvement while working — flag it or fix it without waiting to be asked
- Suggest unit tests when adding logic that could silently break (parsers, calculators, API callers)
- If a task has risk or ambiguity, state my assumption before acting, not after

### End of session
- When the user signals they're done (says "bye", "done", "that's it", etc.), run the `/done` checklist automatically:
  1. Commit and push any uncommitted changes
  2. Update memory with next steps
  3. Print a brief summary: what was done, what's next, open questions
- The user should do as little as possible — I handle the wrap-up

### Memory
- Save decisions, preferences, and next steps to memory files as they happen
- Keep the "Next Session Priorities" in MEMORY.md current after every session
- If the user says "remember X", save it immediately to the right memory file

### Always finish the job
- Never leave tasks half-done and tell the user "there are still things remaining"
- If a session has multiple tasks, complete them all in one run before responding
- If something is blocked or uncertain, state the blocker and complete everything else — don't stop
