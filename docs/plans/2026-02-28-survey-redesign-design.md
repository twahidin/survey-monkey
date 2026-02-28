# Survey Chatbot Redesign — Design Document

Date: 2026-02-28

## Overview

Complete redesign of the survey chatbot with: agentic tool-use flow (Claude shows images, videos, and button options dynamically), glassmorphism UI, responsive mobile-first layout with adaptive media panel, and admin response analytics dashboard.

## Approach

**Tool-Use API (Approach 1):** Claude uses Anthropic tool_use to call `show_image`, `show_buttons`, and `show_video` tools. Backend processes tool calls (fetching media from Unsplash/Pexels), streams text via SSE, and sends structured tool results as additional SSE event types. Frontend renders these as rich UI components.

## Survey Chatbot UI

### Layout

**Mobile (< 768px):** Single column. Media panel slides down from top (top ~1/3 of screen) when Claude sends media. User dismisses with X button. Chat occupies remaining space. Input bar pinned to bottom.

**Desktop/iPad (>= 768px):** Side-by-side. Media panel appears on the left when media is present, chat on the right. Panel collapses when no media. Input bar spans full width at bottom.

### Button Options in Chat

- Single-select: pill buttons in a flex-wrap grid inside the chat flow
- Multi-select: same layout with checkboxes, plus a "Submit" button
- After selection: buttons disable, selected option highlighted with checkmark
- User's selection sent as the next chat message text

### Removed

- Typing monkey mascot — removed entirely
- Replaced with three-dot wave typing indicator

## Visual Design — Glassmorphism Light

- **Background:** Linear gradient #e8f0fe to #f0e6ff (soft blue-to-lavender)
- **Glass cards:** rgba(255,255,255,0.6) + backdrop-filter: blur(20px), border: 1px solid rgba(255,255,255,0.3)
- **Primary accent:** #6366f1 (indigo)
- **Bot messages:** Frosted glass cards (white 70% opacity, blur)
- **User messages:** Solid indigo gradient (#6366f1 to #8b5cf6)
- **Button options:** Glass pill buttons with hover glow + scale effect
- **Media panel:** Frosted glass overlay with rounded corners
- **Input bar:** Glass card pinned to bottom, subtle inner shadow
- **Join screen:** Centered glass card, animated underline focus, floating gradient orbs (CSS)
- **Completion screen:** Animated CSS checkmark draw, subtle confetti particles (CSS)
- Progress indicator in header: message X of Y

## Tool-Use Architecture

### Tools

```
show_image(query, caption?)
  → Fetches from Unsplash API
  → Returns URL + alt text

show_buttons(question, options[], allow_multiple)
  → options: [{label, value}], 2-6 items
  → allow_multiple: single vs multi-select

show_video(query, caption?)
  → Fetches from Pexels API
  → Returns video URL for inline playback
```

### SSE Event Types (additions)

```
{ t: "media", type: "image"|"video", url, caption }
{ t: "buttons", question, options: [{label,value}], allow_multiple }
```

### System Prompt Addition

Appended to survey system prompt: instructions telling Claude about available tools and when to use them (use show_buttons for discrete choices, show_image for visual context, show_video sparingly for demonstrations).

## Admin Dashboard — Response Analytics

### New "Insights" Tab

- **Sentiment overview:** Horizontal bar chart (CSS) showing positive/neutral/negative distribution
- **Top themes:** Ranked list with frequency counts
- **Engagement distribution:** CSS bar histogram of messages per participant
- **Enhanced participant table:** Sortable columns, sentiment + engagement scores with color coding (green/yellow/red), click to view conversation

### How Insights Are Generated

- Backend calls Claude with all transcripts, requests structured JSON analysis
- Per-participant: sentiment (positive/neutral/negative), engagement score (1-10), key themes
- Aggregate: sentiment distribution, ranked themes, response length stats
- Cached in `survey_insights` DB table, re-generated on demand or if stale (>5 min)

### Admin UI Treatment

Same glassmorphism style as survey UI — frosted glass cards, gradient background, consistent visual language.

## Data Model Changes

### New Table: survey_insights

| Column | Type |
|---|---|
| id | UUID (PK) |
| survey_id | UUID (FK → surveys) |
| insights_json | TEXT |
| generated_at | DateTime |

## API Changes

### Modified

- `POST /api/survey/chat/stream` — tool_use in Claude call, new SSE event types

### New

- `GET /api/surveys/{id}/insights` — returns cached or generates fresh insights
- `POST /api/surveys/{id}/insights/regenerate` — force re-generate

## Environment Variables (new)

- `UNSPLASH_ACCESS_KEY` — image search
- `PEXELS_API_KEY` — video search

## Files Changed

- `models.py` — add SurveyInsight model
- `database.py` — migration for new table
- `main.py` — modify chat/stream, add insights endpoints, tool handling
- `templates/survey.html` — complete rewrite
- `templates/admin.html` — complete rewrite
- `templates/typing_monkey_chatbot.html` — delete
