from agent.cosmos.contracts.rpc import Hello, RpcRequest, parse_rpc_line


def test_call_round_trip() -> None:
    message = RpcRequest(id="req-1", method="deliver", params={"text": "在吗"})
    parsed = parse_rpc_line(message.model_dump_json())
    assert parsed == message


def test_hello_round_trip() -> None:
    message = Hello(token="t", contract_version="1.0", client_id="aliya")
    parsed = parse_rpc_line(message.model_dump_json())
    assert parsed == message
