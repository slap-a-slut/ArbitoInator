import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, Tuple

from web3 import Web3


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_artifact() -> Tuple[Dict[str, Any], str]:
    candidates = [
        PROJECT_ROOT / "deploy" / "artifacts" / "ArbitrageExecutor.json",
        PROJECT_ROOT / "out" / "ArbitrageExecutor.sol" / "ArbitrageExecutor.json",
        PROJECT_ROOT / "artifacts" / "contracts" / "ArbitrageExecutor.sol" / "ArbitrageExecutor.json",
    ]
    for path in candidates:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data, str(path)
    raise FileNotFoundError("no contract artifact found (run your compiler and place JSON artifact)")


def deploy() -> None:
    parser = argparse.ArgumentParser(description="Deploy ArbitrageExecutor to a testnet")
    parser.add_argument("--rpc", default=os.getenv("RPC_URL", ""), help="RPC URL")
    parser.add_argument("--private-key", default=os.getenv("PRIVATE_KEY", ""), help="deployer private key")
    parser.add_argument("--chain-id", type=int, default=0, help="chain id (optional)")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "deploy" / "deployed.json"), help="output file")
    args = parser.parse_args()

    rpc_url = str(args.rpc or "").strip()
    private_key = str(args.private_key or "").strip()
    if not rpc_url:
        raise SystemExit("missing --rpc (or RPC_URL)")
    if not private_key:
        raise SystemExit("missing --private-key (or PRIVATE_KEY)")

    artifact, artifact_path = _load_artifact()
    abi = artifact.get("abi")
    bytecode = artifact.get("bytecode") or artifact.get("deployedBytecode")
    if not abi or not bytecode:
        raise SystemExit(f"artifact missing abi/bytecode: {artifact_path}")

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise SystemExit("rpc not connected")

    acct = w3.eth.account.from_key(private_key)
    chain_id = int(args.chain_id or w3.eth.chain_id)
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = contract.constructor().build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "chainId": chain_id,
            "gas": 2_000_000,
            "maxFeePerGas": w3.to_wei("30", "gwei"),
            "maxPriorityFeePerGas": w3.to_wei("2", "gwei"),
        }
    )
    signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    out = {
        "address": receipt.contractAddress,
        "tx_hash": tx_hash.hex(),
        "chain_id": chain_id,
        "deployer": acct.address,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    deploy()
