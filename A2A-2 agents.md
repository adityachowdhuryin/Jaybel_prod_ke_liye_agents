
**System Architecture & Implementation Guide: Enterprise Cost & Usage Metrics A2A System**

**1. Executive Summary** This document outlines the architecture and phased build plan for an enterprise-grade AI Personal Assistant specializing in Cost and Usage Metrics. The system utilizes a "Hybrid Agent Mesh" pattern deployed on GCP Vertex AI. It features a conversational orchestrator that routes user queries to an independent specialist agent via the open A2A Protocol.

**Crucial Architecture Modification:** This implementation uses a **local PostgreSQL database** instead of a managed cloud database, requiring a secure tunnel to connect GCP serverless components to the on-premises database environment.

**2. Technology Stack**

\
  **Frontend:** Next.js 14, Tailwind CSS, shadcn/ui (deployed on GCP Cloud Run).

- **Agent Framework:** Google Agent Development Kit (ADK).

- **Orchestrator Runtime:** Vertex AI Agent Engine.

- **Specialist Runtime:** Vertex AI Agent Engine (deployed independently).

- **Agent Communication:** A2A Protocol (Agent-to-Agent) via Server-Sent Events (SSE).

- **LLMs:** \* Claude Sonnet 4.6 on Vertex AI (Orchestrator, complex reasoning).
  - Claude Haiku 4.5 on Vertex AI (Intent classification, routing).

- **State & Memory:** Vertex AI Agent Engine Sessions (short-term) and Memory Bank (long-term).
- **Persistent Store:** PostgreSQL 18 (Local/On-Premises deployment) replacing Cloud SQL.
- **Network Ingress (Local DB):** Secure tunnel (e.g., Ngrok, Cloudflare Tunnel, or Tailscale) exposing local port 5432to GCP.

**3. Network & Database Architecture (Hybrid Cloud-to-Local)**

Because the persistent store is hosted locally while the compute layer (Agent Engine, Cloud Run) is serverless on GCP, Cursor must implement the following network logic:

\
  **Local Development:** The local ADK runner and Next.js app will connect directly to localhost:5432.
- **Production Deployment:** GCP services will communicate with the database via a provided secure tunnel URL.
- **Environment Variable Handling:** Cursor must ensure the DATABASE\_URL environment variable dynamically switches between localhost for dev and the tunnel URL for production.

**4. Core System Components**

**A. PA Orchestrator (Conversational Brain)**

- Built with Google ADK in multi-agent orchestration mode.

- **Function:** Manages the ReAct loop, reasons over user intent, decides to answer directly or invoke the Cost Metrics agent, dispatches A2A tasks, and streams SSE chunks back to the client WebSocket.

- **State:** Relies entirely on Agent Engine for session state; no custom session store is required.

**B. Cost & Usage Metrics Agent (Specialist)**

- An independent ADK agent running on its own Agent Engine instance.
- **Function:** Connects to the local PostgreSQL database to query financial/usage logs, summarizes findings, and streams them back to the Orchestrator.

**C. The A2A Contract (Discovery & Streaming)**

Cursor should try to implement the following A2A specifications:

1\. Agent Discovery (/.well-known/agent.json) The Specialist agent must expose this exact structure:

JSON

{

`  `"name": "Cost Metrics Agent",

`  `"description": "Enterprise tasks: query infrastructure costs, analyze usage spikes, generate budget reports.",

`  `"url": "https://[COST\_AGENT\_CLOUD\_RUN\_URL]",

`  `"version": "1.0.0",

`  `"capabilities": { "streaming": true, "pushNotifications": false },

`  `"skills": [

`    `{ 

`      `"id": "metrics.query\_cost", 

`      `"name": "Cost Query",

`      `"description": "Query costs by service, date, or environment.",

`      `"inputModes": ["text"], 

`      `"outputModes": ["text"] 

`    `}

`  `]

}

2\. A2A Task Streaming (POST /tasks/send) The interaction must handle SSE streams formatted as:

JSON

// Working chunk streamed to Orchestrator

{"id":"task-abc123","status":{"state":"working","message": {"role":"agent","parts":[{"text":"Analyzing cost data for March..."}]}}}

// Completion chunk

{"id":"task-abc123","status":{"state":"completed"}, "artifact":{"parts":[{"text":"Total cost is $1,450. The largest spike was..."}]}}

**5. Phased Implementation Plan**

Instruct Cursor to execute the build in the following sequence:

**Phase 1: Foundation (Local Dev & Basic A2A)**

\
   **Database Init:** Spin up a local PostgreSQL Docker container (docker run -d --name pg-dev -p 5432:5432 postgres:18). Initialize the schema for enterprise cost logs.
1. **Specialist Agent (Python/ADK):** Build the Cost Metrics Agent. Implement the database connection and the /.well-known/agent.json endpoint. Expose the /tasks/send endpoint for A2A compliance.

1. **Orchestrator Agent (Python/ADK):** Build the PA Orchestrator in multi-agent mode. Configure it to dynamically read the Specialist's Agent Card on startup.

1. **Frontend (Next.js):** Scaffold the UI with shadcn/ui. Implement a WebSocket/SSE connection to stream responses from the Orchestrator progressively.

1. **Local Testing:** Use the adk run command to spin up both agents locally on different ports (e.g., 8000 and 8001). Ensure ADK's built-in, in-memory session service handles local state.

**Phase 2: Hybrid Cloud Deployment & Observability**

1. **Network Tunneling:** Establish the secure tunnel (Ngrok/Cloudflare) pointing to localhost:5432.

1. **Agent Engine Deployment:** Deploy both the Orchestrator and the Cost Metrics Agent to Vertex AI Agent Engine. Inject the tunnel URL into the Specialist Agent's environment variables.

1. **Frontend Deployment:** Deploy the Next.js app to GCP Cloud Run.

1. **Observability Check:** Verify that Agent Engine Tracing is capturing LLM invocations and A2A network calls in the GCP Console.

**Phase 3: Proactive Workflows**

\
   **Cloud Scheduler:** Configure GCP Cloud Scheduler to hit the Orchestrator's HTTP endpoint daily at a set time (e.g., 8:00 AM).
1. **Alerting Logic:** The Orchestrator queries the Cost Agent. If anomalies are found, generate a report.
1. *Constraint Check:* The local database and tunnel must be active when the cron job fires.

**Phase 4: Advanced Intelligence**

\
   **Model Routing:** Implement logic in the Orchestrator to use Claude Haiku 4.5 for simple routing and Claude Sonnet 4.6 for deep financial analysis.

1. **Context Compression:** Implement a summarization routine for long conversational threads to manage token context window sizes.

