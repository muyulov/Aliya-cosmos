import pytest

from agent.aliya.ports import ControlPort, InProcessControl
from agent.cosmos.contracts import CosmosCapabilities


class FakeControl:
    async def handshake(self) -> CosmosCapabilities:
        return CosmosCapabilities(api_version="1.0", contract_version="1.0.0")


def test_in_process_control_satisfies_protocol() -> None:
    port: ControlPort = InProcessControl(FakeControl())  # type: ignore[arg-type]
    assert port is not None


@pytest.mark.asyncio
async def test_handshake() -> None:
    port = InProcessControl(FakeControl())  # type: ignore[arg-type]
    caps = await port.handshake()
    assert caps.contract_version == "1.0.0"
