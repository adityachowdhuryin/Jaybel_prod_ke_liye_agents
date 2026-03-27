"""
Phase 2 observability: Cloud Trace for FastAPI + outbound httpx (A2A calls to Cost Agent).
Set ENABLE_CLOUD_TRACE=1 and GOOGLE_CLOUD_PROJECT on Cloud Run (see deploy.sh).
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI

logger = logging.getLogger(__name__)


def setup_observability(app: FastAPI, service_name: str) -> None:
    if os.environ.get("ENABLE_CLOUD_TRACE", "").lower() not in ("1", "true", "yes"):
        return
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    if not project:
        logger.warning(
            "ENABLE_CLOUD_TRACE is set but GOOGLE_CLOUD_PROJECT is missing; skipping Cloud Trace"
        )
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:
        logger.warning("OpenTelemetry deps missing (%s); skipping Cloud Trace", e)
        return

    HTTPXClientInstrumentor().instrument()

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "hybrid-agent-mesh",
        }
    )
    exporter = CloudTraceSpanExporter(project_id=project)
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    logger.info("Cloud Trace + httpx instrumentation for %s project=%s", service_name, project)
