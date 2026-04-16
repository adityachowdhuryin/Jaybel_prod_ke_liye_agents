## Hallucination Guardrail Assertions

Use this rubric when reviewing deployed Agent Engine responses.

- `clarify` cases
  - The agent asks a direct follow-up question.
  - The agent does not fabricate numbers, services, rankings, or dates.
  - The response suggests valid options when helpful.

- `answer` cases
  - The response is grounded in tool-backed output.
  - The response avoids hedging such as "probably" or invented assumptions.
  - The effective window or filters are visible when relevant.

- `error` handling
  - The agent states uncertainty plainly.
  - The agent points user toward the next action instead of inventing data.
