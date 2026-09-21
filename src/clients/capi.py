import asyncio
import json
from typing import Any

import aiohttp

from processing.log_config import logger
from processing.settings import Settings

#################################################################################
#                        Setting up the global variables                        #
#################################################################################
# Retry/timeout budget. These must fit within the outer wait_for budget applied
# by the caller (ucm_retriever.REQUEST_TIMEOUT); otherwise the outer timeout
# cancels the call mid-flight and the retries never run. With 2 attempts, a 5s
# per-attempt timeout and a 1s backoff, the worst case is ~11s, which sits inside
# the caller's 12s budget.
MAX_RETRIES = 2
RETRY_DELAY = 1  # Initial backoff delay in seconds
REQUEST_TIMEOUT_SECONDS = 5  # Per-attempt HTTP timeout
#################################################################################


class MaxRetriesExceededError(Exception):
    """Raised when a network operation fails after all retries."""

    pass


def construct_ucms_payload(urls: list[str]) -> str:
    """Construct the payload for retrieving multiple UCMs."""
    payload = {
        "booleanFilter": {
            "filters": [
                {
                    "ranges": [
                        {
                            "field": "metadata.extensions.value",
                            "value": url,
                            "operator": "EQ",
                        }
                        for url in urls
                    ],
                    "operator": "OR",
                },
            ],
            "operator": "AND",
        },
        "page": {
            "from": 0,
            "size": len(urls),
        },
    }
    return json.dumps(payload)


async def get_ucms(
    urls: list[str],
    session: aiohttp.ClientSession,
    settings: Settings,
) -> dict[str, Any]:
    """Asynchronously get UCM data for multiple URLs with robust retries.

    Both the session and the settings are supplied by the caller, which owns their
    lifecycle: one process-wide session and one settings instance, created at startup
    and reused for every call (or every request, once this runs behind FastAPI).
    """
    CONTENT_READER_APIGEE_KEY = settings.content_reader_apigee_key
    CAPI_URL = settings.capi_url

    json_payload = construct_ucms_payload(urls)
    headers = {
        "Content-Type": "application/json",
        "app_key": CONTENT_READER_APIGEE_KEY,
    }

    # Use a single session for all retries
    for attempt in range(MAX_RETRIES):
        try:
            # Set a reasonable timeout for the request
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            async with session.post(
                CAPI_URL,
                data=json_payload,
                headers=headers,
                timeout=timeout,
            ) as response:
                response.raise_for_status()
                response_json = await response.json()
                ucm_data_by_url = {}
                for hit in response_json.get("hits", []):
                    url_ext = next(
                        (
                            ext["value"]
                            for ext in hit.get("metadata", {}).get("extensions", [])
                            if ext.get("key") == "url"
                        ),
                        None,
                    )
                    if url_ext:
                        ucm_data_by_url[url_ext] = hit

                return ucm_data_by_url

        except aiohttp.ClientResponseError as e:
            # Handle HTTP status code errors
            # Retry on server errors (5xx) or rate limiting (429)
            if e.status >= 500 or e.status == 429:
                logger.warning(
                    f"Attempt {attempt + 1}/{MAX_RETRIES} failed with HTTP {e.status}. Retrying...",
                )
            else:
                # Fail fast on other client errors (e.g., 400, 401, 403)
                logger.error(
                    f"Non-retryable HTTP {e.status} error for CAPI request: {e.message}",
                )
                raise e  # Re-raise the exception to stop the process
        except (TimeoutError, aiohttp.ClientConnectionError):
            # Handle network/connection/timeout errors
            logger.warning(
                f"Attempt {attempt + 1}/{MAX_RETRIES} failed with connection/timeout error. Retrying...",
            )

        # If we are here, it means a retryable error occurred. Wait before the next attempt.
        if attempt < MAX_RETRIES - 1:
            await asyncio.sleep(RETRY_DELAY * (2**attempt))  # Exponential backoff

    # If the loop finishes without returning, all retries have failed.
    raise MaxRetriesExceededError(f"CAPI request failed after {MAX_RETRIES} attempts.")
