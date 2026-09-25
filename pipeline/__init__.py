"""Daily earned media roundup — local pipeline.

Local stand-in for the Azure Function architecture:
- Excel SME list → roster (instead of Azure Table Storage)
- SQLite for sent-opportunity fingerprints (instead of Table Storage)
- Claude API + web search for research
- Local outbox files for email (instead of Microsoft Graph, for now)
"""

__version__ = "0.1.0"
