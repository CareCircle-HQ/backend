You are tasked with determining the best way to extract data from the "Screening" tab of an external CRM (United US) within our Chrome extension.

IMPORTANT RULES:
- Do NOT write any code.
- Focus entirely on analysis, debugging strategy, and solution design.
- Think like a senior engineer diagnosing a hard scraping problem.
- The goal is to identify the most reliable and maintainable extraction method.

CONTEXT:
- The chrome extension is already successfully scraping other tabs.
- The "Screening" tab is not working correctly.
- The CRM likely uses modern frontend frameworks (React, Vue, etc.) with dynamic rendering.
- Data reliability and consistency are critical.
- screening questions are loaded dynamically and may be in a shadow DOM or iframe.
- check if you can get the data from the api or if it's loaded dynamically.

---

## TASK

### 1. Problem Understanding
- Explain why the Screening tab might behave differently.
- Identify likely technical differences between working tabs and this one.

---

### 2. Debugging & DevTools Investigation Plan

Provide a detailed checklist of what to inspect in Chrome DevTools, including:

#### DOM Inspection
- Whether elements exist immediately or appear after delay
- If content changes after user interaction
- Signs of:
  - Shadow DOM (`#shadow-root`)
  - iframes
  - dynamically generated class names
  - hidden or virtualized elements

#### Network Tab Analysis
- Look for:
  - XHR / fetch requests when opening the Screening tab
  - API endpoints returning the needed data
  - GraphQL requests
- Identify:
  - Request URLs
  - Headers (auth tokens, cookies)
  - Response structure (JSON shape)

#### Timing & Rendering Signals
- Does data load:
  - On tab click?
  - On scroll?
  - After a delay?
- Look for:
  - loading spinners
  - skeleton UI
  - lazy loading / pagination

#### JavaScript Context
- Check if data exists in:
  - global variables (window.*)
  - React DevTools (component props/state)
- Inspect event listeners tied to tab activation

#### Extension Context Issues
- Determine if the issue is:
  - content script running too early
  - script not re-running on tab switch
  - restricted access to iframe content (CORS / sandboxing)

---

### 3. Possible Extraction Approaches

Evaluate ALL viable methods:

- DOM scraping (query selectors)
- Waiting strategies (MutationObserver, polling, event-driven)
- Intercepting API requests (XHR/fetch)
- Reverse-engineering backend endpoints
- Accessing iframe content (if allowed)
- Injecting scripts into page context
- Reading in-memory JS state

For each:
- How it works
- When it succeeds/fails
- Pros/cons in THIS scenario

---

### 4. Root Cause Hypotheses

List the most likely reasons the current solution fails, such as:
- Elements not present when scraped
- Data rendered outside accessible DOM
- API-driven UI not reflected in HTML
- Context isolation issues (content script vs page)
- Iframe or shadow DOM barriers

---

### 5. Recommended Solution

- Choose the BEST approach (or hybrid)
- Justify based on:
  - reliability
  - performance
  - resistance to UI changes
  - ease of maintenance

---

### 6. High-Level Execution Plan (NO CODE)

- Step-by-step description of:
  - when scraping should trigger
  - how data is captured
  - how extension interacts with the page
- Include timing and lifecycle considerations

---

### 7. Risk & Fallback Strategy

- Identify failure points
- Suggest fallback methods if primary strategy fails
- Include monitoring/debugging hooks

---

## OUTPUT REQUIREMENTS
- No code snippets
- Structured and detailed
- Use clear technical reasoning
- Prioritize real-world debugging insights over theory


🔥 Why This Version is Strong for Windsurf
This version:

Forces the agent to actively debug, not just theorize
Includes exact DevTools actions (what to click, inspect, compare)
Covers real Chrome extension pitfalls:

content script timing
iframes
SPA rendering
API interception


Pushes toward API-based scraping (usually the best solution) if available