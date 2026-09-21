from __future__ import annotations

import logging
import os
from contextvars import ContextVar

# Define module-level constants
DEFAULT_LOG_FORMAT = "%(asctime)s - %(unique_id)s - %(levelname)s - %(message)s"
DEFAULT_UNIQUE_ID = "-"
PROD_ENV = "prod"

# Create a context variable to store the request ID
unique_id_var: ContextVar[str] = ContextVar("unique_id", default=DEFAULT_UNIQUE_ID)


class UniqueIdFilter(logging.Filter):
    """
    A custom logging filter that adds a unique identifier to log records.

    This filter injects a unique ID from a ContextVar into each log record,
    allowing for request tracking across asynchronous operations.

    Args:
        name (str, optional): The name of the filter. Defaults to empty string.

    Examples:
        >>> logger = logging.getLogger(__name__)
        >>> logger.addFilter(UniqueIdFilter())
        >>> unique_id_var.set('123')
        >>> logger.info('Test message')  # Will include '123' in the output
    """

    def __init__(self, name: str = "") -> None:
        """Initialize the UniqueIdFilter.

        Args:
            name (str, optional): Name of the filter. Defaults to empty string.
        """
        super().__init__(name)

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter method to process each log record.

        Adds the unique_id from context to the log record.

        Args:
            record (logging.LogRecord): The log record to process.

        Returns:
            bool: Always returns True to allow the record to be processed.
        """
        record.unique_id = unique_id_var.get()  # type: ignore
        return True


def setup_logger() -> logging.Logger:
    """Configure and return a logger instance with standardized settings.

    This function creates a logger with the following features:
    - Automatic log level selection based on environment
    - Unique request ID tracking
    - Standardized log formatting
    - Stream handler configuration
    - Singleton handler pattern

    Returns:
        logging.Logger: A configured logger instance.

    Raises:
        OSError: If environment variable access fails.

    Examples:
        >>> logger = setup_logger()
        >>> logger.info("Application started")
        2024-01-01 12:00:00,000 - - - INFO - Application started
    """
    logger = logging.getLogger(__name__)

    try:
        environment: str | None = os.getenv("ENVIRONMENT")
        log_level: int = logging.WARNING if environment == PROD_ENV else logging.INFO
    except OSError as e:
        log_level = logging.WARNING  # Fallback to WARNING on environment access error
        logger.error(f"Error accessing environment variables: {e}")

    logger.setLevel(log_level)

    # Prevent duplicate handlers
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(log_level)

        formatter = logging.Formatter(DEFAULT_LOG_FORMAT)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    # Add unique ID filter and disable propagation
    logger.addFilter(UniqueIdFilter())
    logger.propagate = False

    return logger


# Create a single logger instance (singleton pattern)
logger: logging.Logger = setup_logger()
