# Incident Triage - GCP Production Architecture

This diagram shows how the proof of concept can scale to thousands of
operational incidents per day on Google Cloud. It keeps the AI workflow
auditable: every Gemini call is schema-constrained, every risky case can defer
to a reviewer, and reviewer decisions feed the evaluation loop.

```mermaid
%%{init: {
  "theme": "base",
  "themeVariables": {
    "fontFamily": "Inter, Segoe UI, Arial, sans-serif",
    "fontSize": "19px",
    "primaryTextColor": "#0f172a",
    "lineColor": "#334155"
  },
  "flowchart": {
    "htmlLabels": true,
    "nodeSpacing": 85,
    "rankSpacing": 90,
    "curve": "basis"
  }
}}%%
%% GCP incident-triage architecture for GitHub Markdown rendering.
flowchart TB
    classDef source fill:#f8fafc,stroke:#64748b,stroke-width:1.3px,color:#0f172a;
    classDef service fill:#eef2ff,stroke:#4f46e5,stroke-width:1.6px,color:#0f172a;
    classDef gemini fill:#f3e8ff,stroke:#7e22ce,stroke-width:2.2px,color:#0f172a;
    classDef data fill:#dcfce7,stroke:#16a34a,stroke-width:1.6px,color:#052e16;
    classDef human fill:#fef3c7,stroke:#d97706,stroke-width:1.8px,color:#422006;
    classDef failure fill:#fee2e2,stroke:#dc2626,stroke-width:1.8px,color:#450a0a;
    classDef observe fill:#e0f2fe,stroke:#0284c7,stroke-width:1.5px,color:#082f49;

    subgraph Ingest["1. Ingest"]
        direction LR
        SRC["ITSM / Email / API"]:::source
        API["Cloud Run<br/>Ingest API"]:::service
        RAW[("GCS Raw Bucket<br/>CMEK + retention")]:::data
        TOPIC["Pub/Sub<br/>incidents.raw"]:::service
    end

    subgraph Prepare["2. Prepare"]
        direction LR
        WORKER["Cloud Run<br/>Triage Worker"]:::service
        DLP["Cloud DLP<br/>PII inspection"]:::service
        SECRETS["Secret Manager<br/>service credentials"]:::service
    end

    subgraph Reason["3. Reason with Gemini"]
        direction LR
        FLASH["Gemini Flash<br/>primary triage"]:::gemini
        PRO["Gemini Pro<br/>low-confidence fallback"]:::gemini
        GUARD["Pipeline Guardrails<br/>schema + safety overrides"]:::service
    end

    subgraph Route["4. Persist and Route"]
        direction LR
        BQ[("BigQuery<br/>triage.results")]:::data
        OUT["Pub/Sub<br/>incidents.triaged"]:::service
        REVIEW["Human Review Queue<br/>deferred + sampled cases"]:::human
        DLQ["Pub/Sub DLQ<br/>failed messages"]:::failure
    end

    subgraph Operate["5. Monitor and Improve"]
        direction LR
        LOGS["Cloud Logging<br/>structured events"]:::observe
        MON["Cloud Monitoring<br/>latency, errors, deferrals"]:::observe
        JUDGE["Gemini Pro as Judge<br/>nightly quality scoring"]:::gemini
        ALERT["Alerts + On-call<br/>accuracy drop / DLQ backlog"]:::failure
    end

    SRC -->|"incident payload"| API
    API -->|"store original text"| RAW
    API -->|"publish event"| TOPIC

    TOPIC -->|"pull message"| WORKER
    WORKER -.->|"read credentials"| SECRETS
    WORKER -->|"inspect input"| DLP
    DLP -->|"redacted text"| FLASH

    FLASH -->|"valid JSON triage"| GUARD
    FLASH -->|"confidence < 0.60 or unknown"| PRO
    PRO -->|"second-pass triage"| GUARD

    GUARD -->|"all results"| BQ
    GUARD -->|"routing event"| OUT
    GUARD -->|"PII, security, unknown, low confidence"| REVIEW
    WORKER -->|"retry exhausted"| DLQ
    REVIEW -->|"reviewer labels"| BQ

    WORKER -.->|"trace_id, prompt_version, latency"| LOGS
    LOGS --> MON
    BQ -->|"golden set + 2% sample"| JUDGE
    JUDGE -->|"quality regression"| ALERT
    MON -->|"SLO breach"| ALERT
    DLQ -->|"backlog alert"| ALERT
```

## Why This Design

| Area | Choice | Why it fits |
| --- | --- | --- |
| Ingest | Cloud Run + Pub/Sub | Separates request handling from AI processing, so traffic spikes and Gemini quota issues do not drop incidents. |
| Raw storage | Cloud Storage | Keeps original incident text in a controlled bucket with CMEK and retention, instead of bloating BigQuery with sensitive blobs. |
| PII handling | Cloud DLP + local redaction | DLP catches broad sensitive-data patterns; local redaction protects the synchronous path before Gemini. |
| AI model | Vertex AI Gemini Flash, with Pro fallback | Flash handles high-volume classification cheaply; Pro is reserved for low-confidence or ambiguous cases. |
| Structured output | Gemini `response_schema` + Pydantic validation | The model must return the known taxonomy; invalid output retries once, then defers to a human. |
| Human review | Queue for deferred and sampled cases | Explicit deferrals are reviewed, and a small sample of confident cases catches overconfident errors. |
| Evaluation | BigQuery + nightly Gemini-as-judge | Deterministic metrics cover category and priority; judge scoring covers open text like `next_action`. |
| Monitoring | Cloud Logging + Cloud Monitoring | Dashboards track latency, error rate, deferral rate, category mix, and DLQ backlog. |

## Human Review Rules

The pipeline should defer to a reviewer when any of these conditions apply:

- Confidence is below `0.60`.
- Category is `unknown`.
- Category is `security`.
- PII or secrets were detected in the input.
- The model output failed schema validation twice.
- The model reports conflicting signals or insufficient information.

Reviewer labels are written back to BigQuery and become part of the next
evaluation dataset.
