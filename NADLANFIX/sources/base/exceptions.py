"""Source adapter exceptions for NADLANFIX."""

from __future__ import annotations


class SourceAdapterError(Exception):
    """Base exception for source adapter errors."""
    pass


class SourceFetchError(SourceAdapterError):
    """Error during data fetching from external source."""
    pass


class SourceNormalizeError(SourceAdapterError):
    """Error during data normalization."""
    pass


class SourcePersistError(SourceAdapterError):
    """Error during data persistence."""
    pass


class SourceConfigError(SourceAdapterError):
    """Error in source configuration."""
    pass


class SourceAuthError(SourceAdapterError):
    """Authentication error with source."""
    pass


class SourceRateLimitError(SourceAdapterError):
    """Rate limit exceeded for source."""
    pass


class SourceSchemaError(SourceAdapterError):
    """Schema mismatch or migration error."""
    pass
