"""Domain exceptions used by the CRM."""


class ScraperError(Exception):
    """Base class for scraper-specific errors."""


class ConfigurationError(ScraperError):
    """Raised when required configuration is missing or invalid."""
