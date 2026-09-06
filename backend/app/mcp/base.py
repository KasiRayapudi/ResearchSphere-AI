from abc import ABC, abstractmethod
from typing import Dict, Any, List

class BaseMCPConnector(ABC):
    """
    Abstract Base Class for Model Context Protocol (MCP) Connectors.
    """
    def __init__(self, connector_id: str, name: str, provider: str):
        self.connector_id = connector_id
        self.name = name
        self.provider = provider
        self.status = "connected"

    @abstractmethod
    def sync_data(self) -> List[Dict[str, Any]]:
        """Syncs items from external provider into vector chunks format."""
        pass

    @abstractmethod
    def get_status(self) -> Dict[str, Any]:
        """Returns connector health and sync counts."""
        pass
